from django.contrib import admin
from unfold.admin import ModelAdmin
from configurator.models import PricingRuleAssignment


@admin.register(PricingRuleAssignment)
class PricingRuleAssignmentAdmin(ModelAdmin):
    list_display = ("id", "configuration", "rule", "enabled")
    list_filter = ("enabled", "rule", "configuration")
    search_fields = ("rule__name", "configuration__name")
    autocomplete_fields = ["configuration"]

    fieldsets = (
        ("Rule Assignment", {
            "fields": ("configuration", "rule", "enabled"),
        }),
    )
