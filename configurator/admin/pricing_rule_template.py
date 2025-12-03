from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRuleTemplate


@admin.register(PricingRuleTemplate)
class PricingRuleTemplateAdmin(ModelAdmin):
    list_display = ("id", "name", "expression", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "expression")
