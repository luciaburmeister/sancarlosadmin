from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRule
from core.admin.company_admin import CompanyRestrictedAdmin


@admin.register(PricingRule)
class PricingRuleAdmin(ModelAdmin,CompanyRestrictedAdmin):
    list_display = ("id", "name", "expression", "is_active","company")
    list_filter = ( "is_active",)
    search_fields = ("name", "expression")
    autocomplete_fields = ["company"]
    list_select_related = ["company"]

    fieldsets = (
        ("Pricing Rule", {
            "fields": (("company"), "name","label", "expression", ("is_active","show_in_export")),
        }),
    )
