from django.contrib import admin
from django.db import models
from assets.models import AssetTransaction
from unfold.admin import ModelAdmin
from django_json_widget.widgets import JSONEditorWidget
from django.utils.translation import gettext_lazy as _

@admin.register(AssetTransaction)
class AssetTransactionAdmin(ModelAdmin):
    list_display = ("asset", "transaction_type", "from_zone", "to_zone", "date", "created_at", "company")
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
    exclude = ["company"]
    
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
    