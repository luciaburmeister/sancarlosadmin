from company.models import WorkingZone
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from unfold.contrib.filters.admin.autocomplete_filters import AutocompleteSelectMultipleFilter
from django import forms
from django.contrib import admin, messages
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.timezone import localtime
from django.db import models
from django.urls import path, reverse
from django.shortcuts import redirect
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest
from assets.models import (
    AssetTransaction,
    AssetTransactionPause,
    AssetTransactionPhoto,
    AssetTransactionReceipt,
)
from django.contrib.contenttypes.models import ContentType
from assets.models import AssetTransaction, BillingStatus
from assets.constants import MAX_RECEIPT_SPLIT_PARTS
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
from unfold.widgets import UnfoldAdminIntegerFieldWidget, UnfoldAdminSplitDateTimeWidget
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


def _document_status_text(obj):
    generated_at = obj.document_generated_at
    if not obj.document_file:
        return _("Not generated yet: it is created the first time the link is opened.")
    if generated_at and generated_at >= obj.updated_at:
        return _("Generated on %(date)s.") % {"date": date_format(localtime(generated_at), "DATETIME_FORMAT")}
    return _("Updating after the last change; the previous version is served meanwhile.")


def _document_qr_html(obj, document_url_name, regenerate_url_name):
    """Shared "QR + View/Regenerate document" block for AssetTransaction and
    AssetTransactionReceipt's document_qr_preview -- same mechanics, only the
    URL names (and so which object's document_file/public_token) differ."""
    uri = qr(obj.document_url)
    if not uri:
        return "—"
    return format_html(
        '<div style="display:flex;align-items:center;gap:16px;">'
        '<img src="{}" alt="" width="120" height="120" style="background:white;" />'
        '<div>'
        '<div style="display:flex;flex-wrap:wrap;gap:8px;">'
        '<a href="{}" target="_blank" rel="noopener" '
        'class="bg-primary-600 font-medium px-3 py-2 rounded-md text-white whitespace-nowrap">{}</a>'
        '<a href="{}" '
        'class="border border-base-200 dark:border-base-700 font-medium px-3 py-2 rounded-md whitespace-nowrap">{}</a>'
        '</div>'
        '<p class="text-sm text-font-subtle-light dark:text-font-subtle-dark" style="margin-top:12px;">{}</p>'
        '</div>'
        '</div>',
        uri,
        reverse(document_url_name, args=[obj.public_token]),
        _("View document"),
        reverse(regenerate_url_name, args=[obj.pk]),
        _("Regenerate document"),
        _document_status_text(obj),
    )


class RegenerateDocumentAdminMixin:
    """Shared "Regenerate document" button behind document_qr_preview, for
    AssetTransactionAdmin and AssetTransactionReceiptAdmin -- only the task,
    the object whose company template applies, and the change-page redirect
    differ between an AssetTransaction and one of its DI receipts."""

    regenerate_document_url_name = ""
    change_url_name = ""

    def _regenerate_document_task(self, obj):
        raise NotImplementedError

    def _document_template_lookup(self, obj):
        """The object whose company export template renders `obj`."""
        return obj

    def regenerate_document_view(self, request, object_id):
        """The "Regenerate document" button of document_qr_preview."""
        from assets.documents import get_document_template

        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404
        if not self.has_change_permission(request, obj):
            raise PermissionDenied
        if get_document_template(self._document_template_lookup(obj)) is None:
            messages.error(request, _("There is no document template for this company."))
        else:
            self._regenerate_document_task(obj).delay(obj.pk, force=True)
            messages.success(request, _("The document is being regenerated; the link serves the new one in a few seconds."))
        return redirect(reverse(self.change_url_name, args=[object_id]))

    def regenerate_document_url(self):
        """The "<pk>/regenerate-document/" path for this admin's get_urls()."""
        return path(
            "<path:object_id>/regenerate-document/",
            self.admin_site.admin_view(self.regenerate_document_view),
            name=self.regenerate_document_url_name,
        )


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
    fields = ("nro_albaran", "part_display", "document_link", "data", "movement_transaction", "foto_preview", "created_at")
    readonly_fields = ("foto_preview", "created_at", "movement_transaction", "nro_albaran", "data", "part_display", "document_link")
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

    @admin.display(description=_("DI Part"))
    def part_display(self, obj):
        return f"{obj.part_number}/{obj.total_parts}"

    @admin.display(description=_("Document"))
    def document_link(self, obj):
        if obj.pk is None:
            return "—"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a>',
            reverse("asset_transaction_receipt_document", args=[obj.public_token]),
            _("View document"),
        )


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
    """Promotes material_vertido/peso_neto/m3 to typed fields (built by
    ReceiptSchema, shared with the AssetTransaction admin/wizard), synced
    into `data` on save -- each split DI gets its own material, weight and
    volume. m3 is the same key CubaSchema stores on the pickup/movement
    transaction; a DI's own value here overrides it on that DI's document."""

    material_vertido = forms.CharField(required=False)  # replaced in __init__ once company is known
    peso_neto = forms.CharField(required=False)  # replaced in __init__
    m3 = forms.CharField(required=False)  # replaced in __init__

    class Meta:
        model = AssetTransactionReceipt
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["nro_albaran"].widget.attrs.setdefault("placeholder", _("Type the albaran number, e.g. 01083485"))
        self.fields["part_number"] = forms.IntegerField(
            label=self.fields["part_number"].label,
            min_value=1,
            widget=UnfoldAdminIntegerFieldWidget,
            help_text=_("Which part this DI is, e.g. 2 for 2/4. Only correct this by hand to fix a mistake."),
        )
        self.fields["total_parts"] = forms.IntegerField(
            label=self.fields["total_parts"].label,
            min_value=1,
            max_value=MAX_RECEIPT_SPLIT_PARTS,
            widget=UnfoldAdminIntegerFieldWidget,
            help_text=_("Total DI parts for this load, e.g. 4 for 2/4. Only correct this by hand to fix a mistake."),
        )
        if "di_number" in self.fields:
            self.fields["di_number"] = forms.IntegerField(
                label=self.fields["di_number"].label, required=False, min_value=1, widget=UnfoldAdminIntegerFieldWidget,
            )
        transaction = self.instance.transaction if self.instance and self.instance.transaction_id else None
        if transaction is None:
            transaction_id = self.data.get("transaction") if self.is_bound else self.initial.get("transaction")
            if transaction_id:
                transaction = AssetTransaction.objects.filter(pk=transaction_id).select_related("company").first()
        company = transaction.company if transaction is not None else None
        self.fields["material_vertido"] = ReceiptSchema.material_vertido_field(company)
        self.fields["peso_neto"] = ReceiptSchema.peso_neto_field()
        self.fields["m3"] = ReceiptSchema.m3_field()
        if self.instance and self.instance.pk:
            data = self.instance.data or {}
            self.initial.setdefault("material_vertido", data.get("material_vertido", ""))
            self.initial.setdefault("peso_neto", data.get("peso_neto", ""))
            self.initial.setdefault("m3", data.get("m3", ""))

    def save(self, commit=True):
        obj = super().save(commit=False)
        data = dict(obj.data or {})
        data["material_vertido"] = self.cleaned_data.get("material_vertido") or ""
        data["peso_neto"] = self.cleaned_data.get("peso_neto") or ""
        data["m3"] = self.cleaned_data.get("m3") or ""
        obj.data = data
        if commit:
            obj.save()
        return obj


@admin.register(AssetTransactionReceipt)
class AssetTransactionReceiptAdmin(RegenerateDocumentAdminMixin, ModelAdmin):
    form = AssetTransactionReceiptAdminForm
    regenerate_document_url_name = "assets_assettransactionreceipt_regenerate_document"
    change_url_name = "admin:assets_assettransactionreceipt_change"
    list_display = ("nro_albaran", "di_number", "part_display", "transaction", "movement_transaction", "foto_thumbnail", "created_at")
    list_filter = (
        ("transaction__asset", AutocompleteSelectMultipleFilter),
        ("created_at", RangeDateTimeFilter),
    )
    search_fields = ("nro_albaran", "=di_number", "transaction__asset__name", "movement_transaction__asset__name")
    autocomplete_fields = ["transaction", "movement_transaction"]
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
                ("part_number", "total_parts"),
                "di_number",
                "material_vertido",
                "peso_neto",
                "m3",
                "foto",
                "foto_preview",
                "document_qr_preview",
                "created_at",
            )
        }),
        (_("Advanced / Raw Data"), {
            "fields": ("data",),
            "classes": ["collapse"],
            "description": _("material_vertido above is the recommended way to edit this. Raw JSON is kept as a fallback for anything else stored here."),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        """di_number is only hand-editable by a superuser, to correct a
        genuine mistake -- everyone else gets it read-only, same as every
        other DI-worthy model."""
        fields = ["foto_preview", "created_at", "document_qr_preview"]
        if not request.user.is_superuser:
            fields.append("di_number")
        return fields

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

    @admin.display(description=_("DI Part"))
    def part_display(self, obj):
        return f"{obj.part_number}/{obj.total_parts}"

    @admin.display(description=_("Document QR"))
    def document_qr_preview(self, obj):
        return _document_qr_html(obj, "asset_transaction_receipt_document", "admin:assets_assettransactionreceipt_regenerate_document")

    def _regenerate_document_task(self, obj):
        from assets.tasks import generate_asset_transaction_receipt_document

        return generate_asset_transaction_receipt_document

    def _document_template_lookup(self, obj):
        return obj.transaction

    def get_urls(self):
        from assets.views.asset_transaction_wizard import ReceiptSplitWizardView

        urls = super().get_urls()
        custom = [
            path(
                "<int:receipt_id>/split/",
                self.admin_site.admin_view(
                    ReceiptSplitWizardView.as_view(model_admin=self)
                ),
                name="assets_assettransactionreceipt_split",
            ),
            self.regenerate_document_url(),
        ]
        return custom + urls


class AssetTransactionPauseInline(TabularInline):
    """Read-only on the movement's change page -- these are only ever
    created by assets.services.transaction_pause (daily task or the
    historical backfill), never by hand."""
    model = AssetTransactionPause
    fk_name = "movement"
    extra = 0
    fields = ("di_number", "date", "document_link", "created_at")
    readonly_fields = ("di_number", "date", "document_link", "created_at")
    can_delete = False
    show_change_link = True
    tab = True

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Document"))
    def document_link(self, obj):
        if obj.pk is None:
            return "—"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener">{}</a>',
            reverse("asset_transaction_pause_document", args=[obj.public_token]),
            _("View document"),
        )


class AssetTransactionPauseAdminForm(forms.ModelForm):
    class Meta:
        model = AssetTransactionPause
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "date" in self.fields:
            self.fields["date"] = forms.SplitDateTimeField(
                label=self.fields["date"].label, required=False, widget=UnfoldAdminSplitDateTimeWidget,
            )


@admin.register(AssetTransactionPause)
class AssetTransactionPauseAdmin(RegenerateDocumentAdminMixin, ModelAdmin):
    form = AssetTransactionPauseAdminForm
    regenerate_document_url_name = "assets_assettransactionpause_regenerate_document"
    change_url_name = "admin:assets_assettransactionpause_change"
    list_display = ("di_number", "movement", "date", "created_at")
    search_fields = ("=di_number", "movement__asset__name")
    autocomplete_fields = ["movement"]
    list_select_related = ["movement__asset"]

    fieldsets = (
        (None, {
            "fields": ("movement", "di_number", "date", "document_qr_preview", "created_at"),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        """date is only hand-editable by a superuser, to correct it -- same
        rule as di_number everywhere else, just not locked forever since a
        pause's date isn't a sequence number."""
        fields = ["di_number", "document_qr_preview", "created_at"]
        if not request.user.is_superuser:
            fields.append("date")
        return fields

    def save_model(self, request, obj, form, change):
        """A superuser hand-editing date here is the only way it can ever
        change away from what the resolving receipt says -- flag it so
        AssetTransactionPause.save() stops auto-tracking that receipt."""
        if change and "date" in form.changed_data:
            obj.date_overridden = True
        super().save_model(request, obj, form, change)

    @admin.display(description=_("Document QR"))
    def document_qr_preview(self, obj):
        return _document_qr_html(
            obj, "asset_transaction_pause_document",
            "admin:assets_assettransactionpause_regenerate_document",
        )

    def _regenerate_document_task(self, obj):
        from assets.tasks import generate_asset_transaction_pause_document

        return generate_asset_transaction_pause_document

    def _document_template_lookup(self, obj):
        return obj.movement

    def get_urls(self):
        urls = super().get_urls()
        return [self.regenerate_document_url()] + urls

    def has_add_permission(self, request):
        return False


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
        if "di_number" in self.fields:
            self.fields["di_number"] = forms.IntegerField(
                label=self.fields["di_number"].label, required=False, min_value=1, widget=UnfoldAdminIntegerFieldWidget,
            )
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


class HasDiNumberFilter(admin.SimpleListFilter):
    """Filters on whether a DI number was assigned (see AssetTransaction.requires_di_number) --
    lets staff hide every non-DI transaction type (DELIVERY, PORTE_MOVEMENT/DELIVERY, *_HOURS).

    A RECEIPT transaction never carries its own `di_number` -- that lives on
    the linked AssetTransactionReceipt row instead (see
    AssetTransactionReceipt.di_number), so it's checked too. Without this, a
    RECEIPT row always looked like it had no DI here, even though its own
    receipt document always has one."""
    title = _("Has DI")
    parameter_name = "has_di"

    def lookups(self, request, model_admin):
        return [
            ("yes", _("Yes")),
            ("no", _("No")),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        has_di = models.Q(di_number__isnull=False) | models.Q(receipts__di_number__isnull=False)
        if value == "yes":
            return queryset.filter(has_di).distinct()
        if value == "no":
            return queryset.exclude(has_di).distinct()
        return queryset


@admin.register(AssetTransaction)
class AssetTransactionAdmin(RegenerateDocumentAdminMixin, ModelAdmin, ImportExportModelAdmin):
    form = AssetTransactionAdminForm
    regenerate_document_url_name = "assets_assettransaction_regenerate_document"
    change_url_name = "admin:assets_assettransaction_change"
    conditional_fields = TRANSACTION_DATA_CONDITIONAL_FIELDS
    resource_classes = [AssetTransactionResource]
    export_form_class = SelectableFieldsExportForm
    list_display = ("asset", "transaction_type", "di_number", "from_zone", "to_zone", "project_file", "date", "get_employee", "has_firma", "billing_status_badge", "company", "imagen")
    list_filter = (
        ("company", AutocompleteSelectMultipleFilter),
        ("transaction_type", MultipleChoicesDropdownFilter),
        HasDiNumberFilter,
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
    search_fields = ("asset__name", "from_zone__name", "to_zone__name", "notes", "reference_number", "=di_number")
    autocomplete_fields = ["asset", "from_zone", "to_zone", "project_file", "planning"]
    exclude = ["company", "firma"]
    list_select_related = ["from_zone__company", "to_zone__company", "company", "performed_by__company", "asset__asset_type", "project_file__client"]
    list_filter_submit = True
    inlines = [
        AssetTransactionEmailLogInline, AssetTransactionReceiptInline,
        AssetTransactionPhotoInline, RelatedMovementReceiptInline, AssetTransactionPauseInline,
    ]

    def get_readonly_fields(self, request, obj=None):
        """di_number is only hand-editable by a superuser, to correct a
        genuine mistake -- everyone else gets it read-only."""
        fields = [
            "firma_preview", "imagen_link", "document_qr_preview",
            "performed_by", "material_vertido_display", "split_action",
        ]
        if not request.user.is_superuser:
            fields.append("di_number")
        return fields

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
                "reference_number",
                "di_number",
                "material_vertido_display",
                "split_action",
                "notes",
                "document_qr_preview",
            )
        }),
        (_("Type-Specific Data"), {
            "fields": (
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
        return _document_qr_html(obj, "asset_transaction_document", "admin:assets_assettransaction_regenerate_document")

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

    def _queue_transaction_emails(self, request, transaction):
        reply_to = sender_reply_to(request.user)
        client = None
        if transaction.project_file and transaction.project_file.client:
            client = transaction.project_file.client
        elif transaction.asset and transaction.asset.client:
            client = transaction.asset.client

        queued = []
        if client and client.email:
            send_asset_transaction_email_task.delay(transaction.id, reply_to=reply_to)
            queued.append(client.email)

        landfill_email = transaction.landfill_notification_email(require_auto=False)
        if landfill_email and landfill_email.lower() not in {email.lower() for email in queued}:
            transaction.queue_landfill_receipt_email(require_auto=False, reply_to=reply_to)
            queued.append(landfill_email)

        if queued:
            self.message_user(
                request, _("Transaction email queued for %s.") % ", ".join(queued),
            )
        else:
            self.message_user(
                request,
                _("No recipient email found (check the client or the landfill)."),
                level="error",
            )

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        transaction = AssetTransaction.objects.select_related(
            "project_file__client", "asset__client",
        ).get(pk=object_id)
        self._queue_transaction_emails(request, transaction)
        return redirect(reverse("admin:assets_assettransaction_changelist"))

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        transaction = AssetTransaction.objects.select_related(
            "project_file__client", "asset__client",
        ).get(pk=object_id)
        self._queue_transaction_emails(request, transaction)
        return redirect(reverse("admin:assets_assettransaction_change", args=[object_id]))

    def _regenerate_document_task(self, obj):
        from assets.tasks import generate_asset_transaction_document

        return generate_asset_transaction_document

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
            self.regenerate_document_url(),
        ]
        return custom_urls + urls

    @admin.display(description="Material de Vertido")
    def material_vertido_display(self, obj):
        receipt = obj.receipts.first()
        if receipt and receipt.data:
            return receipt.data.get("material_vertido", "—")
        return "—"

    @admin.display(description=_("Split into Multiple DIs"))
    def split_action(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        receipt = obj.receipts.order_by("part_number").first()
        if receipt is None:
            return "—"
        url = reverse("admin:assets_assettransactionreceipt_split", args=[receipt.pk])
        label = (
            _("Split into Multiple DIs") if receipt.total_parts == 1
            else _("Add more DI parts (currently %(n)d)") % {"n": receipt.total_parts}
        )
        return format_html(
            '<a href="{}" class="bg-primary-600 font-medium px-4 py-2.5 rounded-default text-white text-sm inline-block '
            'transition-all hover:bg-primary-700">{}</a>',
            url, label,
        )

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
