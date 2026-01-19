from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRuleTemplate


@admin.register(PricingRuleTemplate)
class PricingRuleTemplateAdmin(ModelAdmin):
    list_display = ("id", "product", "name", "expression", "is_active",)
    list_filter = ("product", "is_active")
    search_fields = ("name", "expression")
    autocomplete_fields = ["product"]

    fieldsets = (
        ("Pricing Rule Template", {
            "fields": ("product", "name","label", "expression", ("is_active","show_in_export")),
        }),
    )
