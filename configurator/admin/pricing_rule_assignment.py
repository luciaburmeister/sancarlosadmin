from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRuleAssignment


@admin.register(PricingRuleAssignment)
class PricingRuleAssignmentAdmin(ModelAdmin):
    list_display = ("configuration", "rule", "enabled")
    list_filter = ("enabled", "rule")
    search_fields = ("configuration__name", "rule__name")
