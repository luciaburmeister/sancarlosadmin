from django.contrib import admin
from django.utils.html import format_html
from django.db import models
from assets.models import AssetTransaction
from unfold.admin import ModelAdmin
from django_json_widget.widgets import JSONEditorWidget
from django.utils.translation import gettext_lazy as _

@admin.register(AssetTransaction)
class AssetTransactionAdmin(ModelAdmin):
    list_display = ("asset", "transaction_type", "from_zone", "to_zone", "date", "has_firma", "company")
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
    exclude = ["company","firma"]
    readonly_fields = ["firma_preview"]
    
    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }

    fieldsets = (
        (_("Asset Transaction"), {
            "fields": (
                ("asset", "transaction_type"),
                ("from_zone", "to_zone"),
                "date",
                "data",
                "notes",
            )
        }),
    )

    @admin.display(boolean=True, description="Firma")
    def has_firma(self, obj):
        return bool(obj.firma)

    @admin.display(description="Firma")
    def firma_preview(self, obj):  # ← ADD THIS METHOD
        if obj.firma:
            return format_html(
                '<img src="data:image/png;base64,{}" style="max-width:400px; border:1px solid #ccc;" />',
                obj.firma
            )
        return "Sin firma"
    
