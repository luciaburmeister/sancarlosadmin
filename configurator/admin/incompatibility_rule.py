from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import IncompatibilityRule


@admin.register(IncompatibilityRule)
class IncompatibilityRuleAdmin(ModelAdmin):
    list_display = (
        "id",
        "material_a",
        "material_b",
        "reason",
    )

    list_filter = ("material_a", "material_b")
    search_fields = ("material_a__name", "material_b__name", "reason")

    fieldsets = (
        ("Incompatibility", {
            "fields": ("material_a", "material_b", "reason"),
        }),
    )
