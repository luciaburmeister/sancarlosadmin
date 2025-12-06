from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import BOMRule


@admin.register(BOMRule)
class BOMRuleAdmin(ModelAdmin):
    list_display = ("id", "product", "material", "quantity_expression")
    list_filter = ("product", "material")
    search_fields = ("material__name", "product__name", "quantity_expression")

    fieldsets = (
        ("BOM Rule", {
            "fields": ("product", "material", "quantity_expression", "length_expression"),
        }),
    )
