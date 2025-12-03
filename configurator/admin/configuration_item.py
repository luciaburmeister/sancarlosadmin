from django.contrib import admin
from unfold.admin import ModelAdmin 
from configurator.models import ConfigurationItem


@admin.register(ConfigurationItem)
class ConfigurationItemAdmin(ModelAdmin):
    list_display = (
        "id",
        "configuration",
        "material",
        "quantity",
        "unit_price",
        "line_total",
        "created_at",
    )

    list_filter = ("created_at", "material")
    search_fields = ("material__name", "configuration__name")

    readonly_fields = ("line_total", "created_at")

    fieldsets = (
        ("Item", {
            "fields": ("configuration", "material", "quantity", "unit_price", "line_total"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )
