from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_json_widget.widgets import JSONEditorWidget
from assets.models import Asset, AssetIdentifier, AssetTransaction, AssetDocument, AssetDocumentType
from unfold.widgets import UnfoldAdminSelectWidget
from django.urls import resolve
from django.utils.html import format_html
from django.http import HttpResponse
from assets.utils import generate_asset_qr_grid
from company.forms import BaseDocumentForm
from unfold.contrib.filters.admin import AutocompleteSelectFilter


class AssetDocumentForm(BaseDocumentForm):
    class Meta:
        model = AssetDocument
        fields = "__all__"


class AssetDocumentInline(StackedInline):
    model = AssetDocument
    form = AssetDocumentForm
    extra = 0
    tab = True
    exclude = ["company"]
    autocomplete_fields = ["document_type"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type",  "file"),
                ("does_not_expire","is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )
@admin.register(AssetDocumentType)
class AssetDocumentTypeAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

class AssetIdentifierInline(TabularInline):
    model = AssetIdentifier
    extra = 1
    tab = True

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "label":
            labels = []
            try:
                resolved = resolve(request.path_info)
                object_id = resolved.kwargs.get("object_id")
                if object_id:
                    asset = Asset.objects.select_related("company").get(pk=object_id)
                    labels = asset.company.asset_identifier_labels
                else:
                    from core.admin.company_access import get_user_companies
                    companies = get_user_companies(request.user)
                    if companies.count() == 1:
                        labels = companies.first().asset_identifier_labels
            except Exception:
                pass

            if labels:
                kwargs["widget"] = UnfoldAdminSelectWidget(
                    choices=[("", "---------")] + [(l, l) for l in labels]
                )
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
                    "data",
                    "notes",
                )
            },
        ),
    )
    


@admin.register(Asset)
class AssetAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = ("name", "asset_type", "client", "provider", "assigned_to", "qr_link", "get_identifiers")
    list_filter = ("asset_type", ("client",AutocompleteSelectFilter), ("provider",AutocompleteSelectFilter), "assigned_to",("company",AutocompleteSelectFilter))
    search_fields = ("name", "identifiers__value")
    inlines = [AssetIdentifierInline, AssetTransactionInline, AssetDocumentInline]
    actions = ["download_qr_grid"]
    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }
    autocomplete_fields = ["asset_type", "client", "company", "current_working_zone", "assigned_to", "provider"]
    readonly_fields = ("qr_code_preview",)
    fieldsets = (
        (
            _("Asset"),
            {
                "fields": (
                    ("name", "asset_type"),
                    ("client", "company"),
                    ("assigned_to", "provider"),
                    ("current_working_zone"),
                    "description",
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
            url = f"/api/assets/{obj.pk}/qr/"
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
            url = f"/api/assets/{obj.pk}/qr/"
            return format_html('<a href="{}" target="_blank"> QR</a>', url)
        return "-"
    qr_link.short_description = _("QR")

    def get_identifiers(self, obj):
        return ", ".join([f"{i.label}: {i.value}" for i in obj.identifiers.all()])
    get_identifiers.short_description = _("Identifiers")

    @admin.action(description=_("Download QR Grid"))
    def download_qr_grid(self, request, queryset):
        """Action to download a grid of QR codes for the selected assets."""
        buffer = generate_asset_qr_grid(queryset)
        if not buffer:
            self.message_user(request, _("No selected assets for generation."), level="error")
            return None
            
        response = HttpResponse(buffer.getvalue(), content_type="image/png")
        response["Content-Disposition"] = 'attachment; filename="assets_qr_grid.png"'
        return response