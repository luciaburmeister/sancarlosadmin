from django.contrib import admin
from django.utils.html import format_html
from django.db import models
from django.urls import path, reverse
from django.shortcuts import redirect
from django.http import HttpRequest
from assets.models import AssetTransaction
from assets.views import AssetTransactionExportView
from unfold.admin import ModelAdmin
from unfold.admin import GenericTabularInline as _GenericTabularInline
from unfold.decorators import action
from django_json_widget.widgets import JSONEditorWidget
from django.utils.translation import gettext_lazy as _
from company.models import EmailLog
from core.tasks import send_asset_transaction_email_task


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


@admin.register(AssetTransaction)
class AssetTransactionAdmin(ModelAdmin):
    list_display = ("asset", "transaction_type", "from_zone", "to_zone", "project_file", "date", "get_employee", "has_firma", "company")
    list_filter = (
        "company",
        "transaction_type",
        "asset__asset_type",
        "asset__client",
        "from_zone",
        "to_zone"
    )
    search_fields = ("asset__name", "from_zone__name", "to_zone__name", "notes")
    autocomplete_fields = ["asset", "from_zone", "to_zone", "project_file"]
    exclude = ["company", "firma"]
    readonly_fields = ["firma_preview", "performed_by"]

    inlines = [AssetTransactionEmailLogInline]

    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }

    actions_row = ["export_transaction_action", "send_email_row_action"]
    actions_detail = ["send_email_detail_action"]

    fieldsets = (
        (_("Asset Transaction"), {
            "fields": (
                ("asset", "transaction_type"),
                "from_zone",
                "to_zone",
                "project_file",
                "date",
                "performed_by",
                "data",
                "notes",
            )
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
        return bool(obj.firma)

    @admin.display(description="Firma")
    def firma_preview(self, obj):
        if obj.firma:
            return format_html(
                '<img src="data:image/png;base64,{}" style="width:400px; height:150px; object-fit:contain; border:1px solid #ccc; background:white;" />',
                obj.firma
            )
        return "Sin firma"

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