from django.contrib import admin
from django.utils.html import format_html
from django.db import models
from django.urls import path, reverse
from django.shortcuts import redirect
from django.http import HttpRequest
from assets.models import AssetTransaction
from assets.views import AssetTransactionExportView
from unfold.admin import ModelAdmin
from unfold.decorators import action
from django_json_widget.widgets import JSONEditorWidget
from django.utils.translation import gettext_lazy as _

@admin.register(AssetTransaction)
class AssetTransactionAdmin(ModelAdmin):
    list_display = ("asset", "transaction_type", "from_zone", "to_zone", "date", "get_employee", "has_firma", "company")
    list_filter = (
        "company",
        "transaction_type",
        "asset__asset_type",
        "asset__client",
        "from_zone",
        "to_zone"
    )
    search_fields = ("asset__name", "from_zone__name", "to_zone__name", "notes")
    autocomplete_fields = ["asset", "from_zone", "to_zone"]
    exclude = ["company", "firma"]  # performed_by removed from exclude so it shows in the form
    readonly_fields = ["firma_preview", "performed_by"]  # readonly so it can't be edited manually

    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }

    actions_row = ["export_transaction_action"]

    fieldsets = (
        (_("Asset Transaction"), {
            "fields": (
                ("asset", "transaction_type"),
                "from_zone",
                "to_zone",
                "date",
                "performed_by",  # who made the movement
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