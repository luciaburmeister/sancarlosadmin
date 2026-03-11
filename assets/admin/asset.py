from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_json_widget.widgets import JSONEditorWidget
from assets.models import Asset, AssetIdentifier, AssetTransaction
from unfold.widgets import UnfoldAdminSelectWidget
from django import forms
from django.urls import resolve, reverse
from django.utils.html import format_html


class AssetIdentifierInline(TabularInline):
    model = AssetIdentifier
    extra = 1
    tab = True

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "label":
            try:
                resolved = resolve(request.path_info)
                object_id = resolved.kwargs.get("object_id")
                if object_id:
                    asset = Asset.objects.select_related("company").get(pk=object_id)
                    labels = asset.company.asset_identifier_labels
                    if labels:
                        kwargs["widget"] = UnfoldAdminSelectWidget(
                            choices=[("", "---------")] + [(l, l) for l in labels]
                        )
            except Exception:
                pass
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class AssetTransactionInline(StackedInline):
    model = AssetTransaction
    extra = 0
    tab = True
    readonly_fields = ("created_at",)
    ordering = ("-date",)
    autocomplete_fields = ["from_zone", "to_zone"]
    fieldsets = (
        (
            _("Transaction"),
            {
                "fields": (
                    ("transaction_type", "date"),
                    ("from_zone", "to_zone"),
                    "notes",
                )
            },
        ),
    )


@admin.register(Asset)
class AssetAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = ("name", "asset_type", "client", "assigned_to", "qr_link", "get_identifiers")
    list_filter = ("asset_type", "client", "assigned_to")
    search_fields = ("name", "identifiers__value")
    inlines = [AssetIdentifierInline, AssetTransactionInline]
    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }
    autocomplete_fields = ["asset_type", "client", "company", "current_working_zone","assigned_to"]
    readonly_fields = ("qr_code_preview",)
    fieldsets = (
        (
            _("Asset"),
            {
                "fields": (
                    ("name", "asset_type"),
                    ("client", "company"),
                    ("current_working_zone"),
                    ("assigned_to"),
                    "data",
                )
            },
        ),
        (
            _("QR Code"),
            {
                "fields": ("qr_code_preview",),
            },
        ),
    )

    def qr_code_preview(self, obj):
        """Shows the QR code image directly in the asset detail page."""
        if obj.pk:
            url = f"/en/api/assets/{obj.pk}/qr/"
            return format_html(
                '<a href="{}" target="_blank">'
                '<img src="{}" style="width:200px; height:200px;" />'
                '<br><small>Haz clic para abrir en pantalla completa e imprimir</small>'
                '</a>',
                url, url
            )
        return _("Guarda el asset primero para ver el QR.")
    qr_code_preview.short_description = _("QR Code")

    def qr_link(self, obj):
        """Shows a small QR link in the asset list."""
        if obj.pk:
            url = f"/en/api/assets/{obj.pk}/qr/"
            return format_html('<a href="{}" target="_blank"> QR</a>', url)
        return "-"
    qr_link.short_description = _("QR")

    def get_identifiers(self, obj):
        return ", ".join([f"{i.label}: {i.value}" for i in obj.identifiers.all()])
    get_identifiers.short_description = _("Identifiers")