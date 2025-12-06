from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRuleTemplate


@admin.register(PricingRuleTemplate)
class PricingRuleTemplateAdmin(ModelAdmin):
    list_display = ("id", "product", "name", "expression", "is_active")
    list_filter = ("product", "is_active")
    search_fields = ("name", "expression")

    fieldsets = (
        ("Pricing Rule Template", {
            "fields": ("product", "name", "expression", "is_active"),
        }),
    )
