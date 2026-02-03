from django.contrib import admin
from unfold.admin import ModelAdmin 
from configurator.models import ConfigurationItem


@admin.register(ConfigurationItem)
class ConfigurationItemAdmin(ModelAdmin):
    list_display = (
        "id",
        "configuration",
        "quantity",
        "unit_price",
        "line_total",
        "created_at",
    )

    list_filter = ("created_at", "component")

    search_fields = ("component__name","configuration__name")

    readonly_fields = ("line_total", "created_at")

    fieldsets = (
        ("Item", {
            "fields": ("configuration", "quantity", "unit_price", "line_total"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )
