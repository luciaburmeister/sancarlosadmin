from company.models import WorkingZone
from unfold.contrib.filters.admin import RangeDateTimeFilter
from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from unfold.contrib.filters.admin.autocomplete_filters import AutocompleteSelectMultipleFilter
from django.contrib import admin, messages
from django.utils.html import format_html
from django.db import models
from django.urls import path, reverse
from django.shortcuts import redirect
from django.http import HttpRequest
from assets.models import AssetTransaction, AssetTransactionReceipt
from django.contrib.contenttypes.models import ContentType
from assets.models import AssetTransaction, BillingStatus
from assets.resources import AssetTransactionResource
from assets.views import AssetTransactionExportView
from import_export.admin import ExportMixin
from unfold.admin import ModelAdmin
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from unfold.admin import GenericTabularInline as _GenericTabularInline,TabularInline
from unfold.decorators import action, display
from django_json_widget.widgets import JSONEditorWidget
from django.utils.translation import gettext_lazy as _
from company.models import EmailLog
from core.tasks import send_asset_transaction_email_task
import logging
from import_export.admin import ImportExportModelAdmin

logger = logging.getLogger(__name__)


class AssetTransactionEmailLogInline(_GenericTabularInline):
    model = EmailLog
    extra = 0
    tab = True
    verbose_name = _("Email Sent")
    verbose_name_plural = _("Emails Sent")
    fields = ("to_email", "subject", "sent_at", "status", "error_message")
    readonly_fields = ("to_email", "subject", "sent_at", "status", "error_message")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class AssetTransactionReceiptInline(TabularInline):
    """Shows receipts (albaranes) attached to this transaction."""
    model = AssetTransactionReceipt
    fk_name = "transaction"
    extra = 0
    fields = ("nro_albaran", "data", "movement_transaction", "foto_preview", "created_at")
    readonly_fields = ("foto_preview", "created_at", "movement_transaction","nro_albaran","data")
    can_delete = False
    show_change_link = True
    verbose_name = "Albaran"
    verbose_name_plural = "Albaranes"
    tab=True

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


@admin.register(AssetTransactionReceipt)
class AssetTransactionReceiptAdmin(ModelAdmin):
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
                "foto",
                "foto_preview",
                "data",
                "created_at",
            )
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


@admin.register(AssetTransaction)
class AssetTransactionAdmin(ModelAdmin, ImportExportModelAdmin):
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
        ("date",RangeDateTimeFilter),
        ("performed_by", AutocompleteSelectMultipleFilter),
        ("asset", AutocompleteSelectMultipleFilter)
    )
    search_fields = ("asset__name", "from_zone__name", "to_zone__name", "notes")
    autocomplete_fields = ["asset", "from_zone", "to_zone", "project_file"]
    exclude = ["company", "firma"]
    readonly_fields = ["firma_preview", "imagen_link", "performed_by", "material_vertido_display", "peso_neto_display"]
    list_select_related = ["from_zone__company", "to_zone__company", "company", "performed_by__company", "asset__asset_type"]
    list_filter_submit = True
    inlines = [AssetTransactionEmailLogInline, AssetTransactionReceiptInline, RelatedMovementReceiptInline]

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
                "date",
                "performed_by",
                "material_vertido_display",
                "peso_neto_display",
                "data",
                "notes",
            )
        }),
        (_("Imagen"), {
            "fields": ("imagen",)
        }),
        (_("Firma"), {
            "fields": ("firma_preview",)
        }),
    )

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
            send_asset_transaction_email_task.delay(transaction.id)
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
            send_asset_transaction_email_task.delay(transaction.id)
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

    @admin.display(description="Peso Neto (kg)")
    def peso_neto_display(self, obj):
        receipt = obj.receipts.first()
        if receipt and receipt.data:
            return receipt.data.get("peso_neto", "—")
        return "—"
    # --- Billing status ---

    @display(
        description=_("Billing"),
        ordering="billing_status",
        label={
            BillingStatus.PROCESSED.label: "success",
            BillingStatus.PENDING.label: "warning",
            BillingStatus.FAILED.label: "danger",
            BillingStatus.NONE.label: None,
        },
    )
    def billing_status_badge(self, obj):
        return obj.get_billing_status_display()


    @action(description=_("Create Work Entry"), icon="add_task")
    def create_work_entry_row_action(self, request, object_id):
        """Create work entries from this transaction for all missing expected types."""
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
            messages.info(request, _("All expected work entries already exist for this transaction."))
            return redirect(reverse("admin:assets_assettransaction_changelist"))

        created = []
        errors = []
        for wtype in status["missing"]:
            try:
                entry = WorkEntryService.create_work_entry_from_source(
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

        # Update billing status on the transaction
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
                "Created %(count)d work entries."
            ) % {"count": len(created)})

        return redirect(reverse("admin:assets_assettransaction_changelist"))

    @action(description=_("Generate All Work Entries"))
    def generate_all_work_entries(self, request, queryset):
        """Bulk action: dispatch work entry generation to Celery."""
        from core.tasks import generate_work_entries_task

        transaction_ids = list(queryset.values_list("pk", flat=True))
        generate_work_entries_task.delay(transaction_ids)

        messages.success(request, _(
            "Work entry generation queued for %(count)d transactions."
        ) % {"count": len(transaction_ids)})
