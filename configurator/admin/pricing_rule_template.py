from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRuleTemplate
from core.admin.company_admin import CompanyRestrictedAdmin


@admin.register(PricingRuleTemplate)
class PricingRuleTemplateAdmin(ModelAdmin,CompanyRestrictedAdmin):
    list_display = ("id", "product", "name", "expression", "is_active","company")
    list_filter = ("product", "is_active")
    search_fields = ("name", "expression")
    autocomplete_fields = ["product","company"]
    list_select_related = ["product","company"]

    fieldsets = (
        ("Pricing Rule Template", {
            "fields": (("product","company"), "name","label", "expression", ("is_active","show_in_export")),
        }),
    )
