from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import BOMRule


@admin.register(BOMRule)
class BOMRuleAdmin(ModelAdmin):
    list_display = ("id", "product", "component", "quantity_expression")
    list_filter = ("product", "component","component__materials")
    search_fields = ("component__materials__name", "product__name", "quantity_expression")
    autocomplete_fields = ["product","component"]

    fieldsets = (
        ("BOM Rule", {
            "fields": (("product", "component"),"description", "quantity_expression", "length_expression"),
        }),
    )
