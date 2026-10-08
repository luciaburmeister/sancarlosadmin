from company.models import WorkingZone
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from unfold.contrib.filters.admin.autocomplete_filters import AutocompleteSelectMultipleFilter
from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html
from django.db import models
from django.urls import path, reverse
from django.shortcuts import redirect
from django.http import HttpRequest
from assets.models import AssetTransaction, AssetTransactionReceipt, AssetTransactionPhoto
from django.contrib.contenttypes.models import ContentType
from assets.models import AssetTransaction, BillingStatus
from assets.resources import AssetTransactionResource
from assets.transaction_data_schema import (
    SCHEMAS,
    TRANSACTION_DATA_CONDITIONAL_FIELDS,
    ReceiptSchema,
    build_data_from_cleaned,
    initial_from_data,
)
from assets.views import AssetTransactionExportView
from configurator.templatetags.formatting import qr
from import_export.admin import ExportMixin
from unfold.admin import ModelAdmin
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from unfold.admin import GenericTabularInline as _GenericTabularInline, TabularInline
from unfold.decorators import action, display
from django_json_widget.widgets import JSONEditorWidget
from django.utils.translation import gettext_lazy as _
from company.models import EmailLog
from core.tasks import send_asset_transaction_email_task
from general.email import sender_reply_to
import logging
from import_export.admin import ImportExportModelAdmin

logger = logging.getLogger(__name__)


class AssetTransactionEmailLogInline(_GenericTabularInline):
    model = EmailLog
    extra = 0
    tab = True
    verbose_name = _("Email Sent")
    verbose_name_plural = _("Emails Sent")
    fields = (
        "to_email", "subject", "sent_at", "status", "delivery_status",
        "delivered_at", "first_opened_at", "last_opened_at", "open_count",
        "delivery_error", "error_message",
    )
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class AssetTransactionReceiptInline(TabularInline):
    model = AssetTransactionReceipt
    fk_name = "transaction"
    extra = 0
    fields = ("nro_albaran", "data", "movement_transaction", "foto_preview", "created_at")
    readonly_fields = ("foto_preview", "created_at", "movement_transaction", "nro_albaran", "data")
    can_delete = False
    show_change_link = True
    verbose_name = "Albaran"
    verbose_name_plural = "Albaranes"
    tab = True

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "movement_transaction__asset__asset_type", "movement_transaction__from_zone__company", "movement_transaction__to_zone__company",
        )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Foto")
    def foto_preview(self, obj):
        if obj.foto:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="width:150px; height:100px; object-fit:cover; border:1px solid #ccc;" /></a>',
                obj.foto.url
            )
        return "Sin foto"


class AssetTransactionPhotoInline(TabularInline):
    model = AssetTransactionPhoto
    extra = 0
    tab = True
    verbose_name = _("Photo")
    verbose_name_plural = _("Photos")
    fields = ("photo_type", "photo_preview", "created_at")
    readonly_fields = ("photo_preview", "created_at", "photo_type")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Photo")
    def photo_preview(self, obj):
        if obj.photo:
            return format_html(
                '<a href="{}" target="_blank"><img src="{}" style="width:200px; height:150px; object-fit:cover; border:1px solid #ccc;" /></a>',
                obj.photo.url, obj.photo.url
            )
        return "Sin foto"


class RelatedMovementReceiptInline(TabularInline):
    """Shows receipts filed elsewhere that reference this transaction as the original movement."""
    model = AssetTransactionReceipt
    fk_name = "movement_transaction"
    extra = 0
    fields = ("transaction", "nro_albaran", "data", "foto_preview", "created_at")
    readonly_fields = ("transaction", "nro_albaran", "data", "foto_preview", "created_at")
    can_delete = False
    show_change_link = True
    verbose_name = "Recibo del Movimiento"
    verbose_name_plural = "Recibos del Movimiento"
    tab = True

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "transaction__asset__asset_type", "transaction__from_zone__company", "transaction__to_zone__company",
        )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Foto")
    def foto_preview(self, obj):
        if obj.foto:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="width:150px; height:100px; object-fit:cover; border:1px solid #ccc;" /></a>',
                obj.foto.url
            )
        return "Sin foto"


class AssetTransactionReceiptAdminForm(forms.ModelForm):
    """Promotes material_vertido to a typed field (built by ReceiptSchema,
    shared with the AssetTransaction admin/wizard), synced into `data` on save."""

    material_vertido = forms.CharField(required=False)  # replaced in __init__ once company is known

    class Meta:
        model = AssetTransactionReceipt
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["nro_albaran"].widget.attrs.setdefault("placeholder", _("Type the albaran number, e.g. 01083485"))
        transaction = self.instance.transaction if self.instance and self.instance.transaction_id else None
        if transaction is None:
            transaction_id = self.data.get("transaction") if self.is_bound else self.initial.get("transaction")
            if transaction_id:
                transaction = AssetTransaction.objects.filter(pk=transaction_id).select_related("company").first()
        company = transaction.company if transaction is not None else None
        self.fields["material_vertido"] = ReceiptSchema.material_vertido_field(company)
        if self.instance and self.instance.pk:
            data = self.instance.data or {}
            self.initial.setdefault("material_vertido", data.get("material_vertido", ""))

    def save(self, commit=True):
        obj = super().save(commit=False)
        data = dict(obj.data or {})
        data["material_vertido"] = self.cleaned_data.get("material_vertido") or ""
        obj.data = data
        if commit:
            obj.save()
        return obj


@admin.register(AssetTransactionReceipt)
class AssetTransactionReceiptAdmin(ModelAdmin):
    form = AssetTransactionReceiptAdminForm
    list_display = ("nro_albaran", "transaction", "movement_transaction", "foto_thumbnail", "created_at")
    list_filter = (
        ("transaction__asset", AutocompleteSelectMultipleFilter),
        ("created_at", RangeDateTimeFilter),
    )
    search_fields = ("nro_albaran", "transaction__asset__name", "movement_transaction__asset__name")
    autocomplete_fields = ["transaction", "movement_transaction"]
    readonly_fields = ["foto_preview", "created_at"]
    list_select_related = ["transaction__asset", "movement_transaction__asset"]
    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }

    fieldsets = (
        (None, {
            "fields": (
                "transaction",
                "movement_transaction",
                "nro_albaran",
                "material_vertido",
                "foto",
                "foto_preview",
                "created_at",
            )
        }),
        (_("Advanced / Raw Data"), {
            "fields": ("data",),
            "classes": ["collapse"],
            "description": _("material_vertido above is the recommended way to edit this. Raw JSON is kept as a fallback for anything else stored here."),
        }),
    )

    @admin.display(description="Foto")
    def foto_thumbnail(self, obj):
        if obj.foto:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="width:60px; height:40px; object-fit:cover; border:1px solid #ccc;" /></a>',
                obj.foto.url
            )
        return "Sin foto"

    @admin.display(description="Foto")
    def foto_preview(self, obj):
        if obj.foto:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" style="width:400px; height:300px; object-fit:contain; border:1px solid #ccc;" /></a>',
                obj.foto.url
            )
        return "Sin foto"


class AssetTransactionAdminForm(forms.ModelForm):
    """Replaces the raw `data` JSON textarea with typed fields, shown/hidden
    by transaction_type (see assets.transaction_data_schema). Keys match what
    the mobile app writes, since work_entry.py reads them for pricing rules.
    `data` stays available (collapsed) as a fallback; typed values merge into
    it on save rather than replacing it."""

    class Meta:
        model = AssetTransaction
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        if instance and instance.pk:
            initial = initial_from_data(instance.transaction_type, instance.data, instance.company)
            for name, value in initial.items():
                if name in self.fields:
                    self.initial.setdefault(name, value)

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.data = build_data_from_cleaned(
            obj.transaction_type, self.cleaned_data, existing_data=obj.data, tx_date=obj.date,
        )
        if commit:
            obj.save()
            self.save_m2m()
        return obj


@admin.register(AssetTransaction)
class AssetTransactionAdmin(ModelAdmin, ImportExportModelAdmin):
    form = AssetTransactionAdminForm
    conditional_fields = TRANSACTION_DATA_CONDITIONAL_FIELDS
    resource_classes = [AssetTransactionResource]
    export_form_class = SelectableFieldsExportForm
    list_display = ("asset", "transaction_type", "from_zone", "to_zone", "project_file", "date", "get_employee", "has_firma", "billing_status_badge", "company", "imagen")
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("transaction_type", MultipleChoicesDropdownFilter),
        ("project_file", AutocompleteSelectMultipleFilter),
        ("asset__asset_type", AutocompleteSelectMultipleFilter),
        ("asset__client", AutocompleteSelectMultipleFilter),
        ("from_zone", AutocompleteSelectMultipleFilter),
        ("to_zone", AutocompleteSelectMultipleFilter),
        ("date", RangeDateTimeFilter),
        ("performed_by", AutocompleteSelectMultipleFilter),
        ("asset", AutocompleteSelectMultipleFilter),
        ("planning", AutocompleteSelectMultipleFilter)
    )
    search_fields = ("asset__name", "from_zone__name", "to_zone__name", "notes")
    autocomplete_fields = ["asset", "from_zone", "to_zone", "project_file", "planning"]
    exclude = ["company", "firma"]
    readonly_fields = [
        "firma_preview", "imagen_link", "document_qr_preview",
        "performed_by", "material_vertido_display",
    ]
    list_select_related = ["from_zone__company", "to_zone__company", "company", "performed_by__company", "asset__asset_type", "project_file"]
    list_filter_submit = True
    inlines = [AssetTransactionEmailLogInline, AssetTransactionReceiptInline, AssetTransactionPhotoInline, RelatedMovementReceiptInline]

    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }

    actions_row = ["export_transaction_action", "send_email_row_action", "create_work_entry_row_action"]
    actions_detail = ["send_email_detail_action"]
    actions = ["generate_all_work_entries"]

    fieldsets = (
        (_("Asset Transaction"), {
            "fields": (
                ("asset", "transaction_type"),
                "from_zone",
                "to_zone",
                "project_file",
                "planning",
                "date",
                "performed_by",
                "material_vertido_display",
                "notes",
                "document_qr_preview",
            )
        }),
        (_("Type-Specific Data"), {
            "fields": (
                "reference_number",
                ("tipo_cuba", "cubaEstado"),
                ("vasAVerter", "m3"),
                ("trabajo", "matricula"),
                ("peso_vertido", "vertedero"),
                ("implemento", "tipo_camion"),
                "load_description",
                ("client_id", "machine_id"),
                ("goods_type", "tonnes_transported"),
                "package_count",
                "nro_albaran",
                "material_vertido",
                ("interval_1_start", "interval_1_end"),
                ("interval_2_start", "interval_2_end"),
                ("interval_3_start", "interval_3_end"),
                ("interval_4_start", "interval_4_end"),
            ),
            "description": _("Shown fields depend on the Transaction Type selected above."),
        }),
        (_("Advanced / Raw Data"), {
            "fields": ("data",),
            "classes": ["collapse"],
            "description": _("Typed fields above are the recommended way to edit this. Raw JSON is kept as a fallback for legacy keys not covered by the typed fields."),
        }),
        (_("Imagen"), {
            "fields": ("imagen",)
        }),
        (_("Firma"), {
            "fields": ("firma_preview",)
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        # Schema fields must be declared on the form class itself, not added in
        # __init__ -- Django validates fieldsets against Meta.fields before
        # __init__ runs. Same fix as AssetAdmin.get_form()/AssetForm: build a
        # fresh dynamic subclass with them as real class attributes each request.
        company = obj.company if obj and obj.pk else None
        dynamic_fields = {}
        # One fields() call per schema, not per transaction type: each call
        # queries ConfigurationList, and get_form runs several times a request.
        for schema in SCHEMAS:
            for name, field in schema.fields(company).items():
                dynamic_fields.setdefault(name, field)
        kwargs["form"] = type(f"Dynamic{self.form.__name__}", (self.form,), dynamic_fields)
        return super().get_form(request, obj, **kwargs)

    @admin.display(description="Empleado")
    def get_employee(self, obj):
        if obj.performed_by:
            return obj.performed_by.full_name
        return "—"

    @admin.display(boolean=True, description="Firma")
    def has_firma(self, obj):
        return bool(obj.firma) or bool(obj.firma_image)

    @admin.display(description="Firma")
    def firma_preview(self, obj):
        if obj.firma_image:
            return format_html(
                '<img src="{}" style="width:400px; height:150px; object-fit:contain; border:1px solid #ccc; background:white;" />',
                obj.firma_image.url
            )
        if obj.firma:
            return format_html(
                '<img src="data:image/png;base64,{}" style="width:400px; height:150px; object-fit:contain; border:1px solid #ccc; background:white;" />',
                obj.firma
            )
        return "Sin firma"

    @admin.display(description=_("Document QR"))
    def document_qr_preview(self, obj):
        uri = qr(obj.document_url)
        if not uri:
            return "—"
        return format_html(
            '<div style="display:flex;align-items:center;gap:16px;">'
            '<img src="{}" alt="" width="120" height="120" style="background:white;" />'
            '<a href="{}" target="_blank" rel="noopener" '
            'class="bg-primary-600 font-medium px-3 py-2 rounded-md text-white whitespace-nowrap">{}</a>'
            '</div>',
            uri,
            reverse("asset_transaction_document", args=[obj.public_token]),
            _("View document"),
        )

    @admin.display(description="Imagen")
    def imagen_link(self, obj):
        if obj.imagen:
            return format_html(
                '<a href="/en/api/assets/transaction/{}/imagen/" target="_blank">Ver imagen</a>',
                obj.pk
            )
        return "no image"

    @action(
        description=_("Export"),
        url_path="assettransaction-export-action",
        attrs={"target": "_blank"},
    )
    def export_transaction_action(self, request: HttpRequest, object_id: int):
        url = reverse("admin:assets_assettransaction_export", args=[object_id])
        return redirect(url)

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        transaction = AssetTransaction.objects.select_related(
            "project_file__client", "asset__client"
        ).get(pk=object_id)

        client = None
        if transaction.project_file and transaction.project_file.client:
            client = transaction.project_file.client
        elif transaction.asset and transaction.asset.client:
            client = transaction.asset.client

        if not client or not client.email:
            self.message_user(request, _("No client email found (check Expediente or Asset client)."), level="error")
        else:
            send_asset_transaction_email_task.delay(
                transaction.id, reply_to=sender_reply_to(request.user)
            )
            self.message_user(request, _("Transaction email queued for %s.") % client.email)
        return redirect(reverse("admin:assets_assettransaction_changelist"))

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        transaction = AssetTransaction.objects.select_related(
            "project_file__client", "asset__client"
        ).get(pk=object_id)

        client = None
        if transaction.project_file and transaction.project_file.client:
            client = transaction.project_file.client
        elif transaction.asset and transaction.asset.client:
            client = transaction.asset.client

        if not client or not client.email:
            self.message_user(request, _("No client email found (check Expediente or Asset client)."), level="error")
        else:
            send_asset_transaction_email_task.delay(
                transaction.id, reply_to=sender_reply_to(request.user)
            )
            self.message_user(request, _("Transaction email queued for %s.") % client.email)
        return redirect(reverse("admin:assets_assettransaction_change", args=[object_id]))

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:transaction_id>/export/",
                self.admin_site.admin_view(
                    AssetTransactionExportView.as_view(model_admin=self)
                ),
                name="assets_assettransaction_export",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Material de Vertido")
    def material_vertido_display(self, obj):
        receipt = obj.receipts.first()
        if receipt and receipt.data:
            return receipt.data.get("material_vertido", "—")
        return "—"

    @display(
        description=_("Billing"),
        ordering="billing_status",
        label={
            BillingStatus.PROCESSED: "success",
            BillingStatus.PENDING: "warning",
            BillingStatus.FAILED: "danger",
            BillingStatus.NONE: None,
        },
    )
    def billing_status_badge(self, obj):
        return obj.billing_status, obj.get_billing_status_display()

    @action(description=_("Create Work Entry"), icon="add_task")
    def create_work_entry_row_action(self, request, object_id):
        from management.services.work_entry import WorkEntryService

        transaction = AssetTransaction.objects.select_related(
            "project_file", "company"
        ).get(pk=object_id)

        if not transaction.project_file:
            messages.error(request, _("This transaction has no Project File linked."))
            return redirect(reverse("admin:assets_assettransaction_changelist"))

        status = WorkEntryService.get_billing_status(transaction)

        if not status["missing"]:
            transaction.billing_status = BillingStatus.PROCESSED
            transaction.save(update_fields=["billing_status"])
            messages.info(request, _("All expected entries already exist for this transaction."))
            return redirect(reverse("admin:assets_assettransaction_changelist"))

        created = []
        errors = []
        for wtype in status["missing"]:
            try:
                entry = WorkEntryService.create_entry_from_source(
                    entry_type=wtype,
                    source_object=transaction,
                    project_file=transaction.project_file,
                )
                created.append(entry)
            except Exception as e:
                logger.exception(
                    "Failed to create work entry '%s' for transaction %s",
                    wtype.name, transaction.pk,
                )
                errors.append(wtype.name)
                messages.error(request, _(
                    "Failed to create entry '%(name)s': %(error)s"
                ) % {"name": wtype.name, "error": str(e)})

        if errors and not created:
            transaction.billing_status = BillingStatus.FAILED
        elif errors:
            transaction.billing_status = BillingStatus.PENDING
        elif status["count_existing"] + len(created) >= status["count_expected"]:
            transaction.billing_status = BillingStatus.PROCESSED
        else:
            transaction.billing_status = BillingStatus.PENDING
        transaction.save(update_fields=["billing_status"])

        if created:
            messages.success(request, _(
                "Created %(count)d entries."
            ) % {"count": len(created)})

        return redirect(reverse("admin:assets_assettransaction_changelist"))

    @action(description=_("Generate All Work Entries"))
    def generate_all_work_entries(self, request, queryset):
        from core.tasks import generate_work_entries_task

        transaction_ids = list(queryset.values_list("pk", flat=True))
        generate_work_entries_task.delay(transaction_ids)

        messages.success(request, _(
            "Work entry generation queued for %(count)d transactions."
        ) % {"count": len(transaction_ids)})
