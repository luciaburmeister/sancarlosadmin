from django.contrib import admin
from unfold.admin import ModelAdmin, StackedInline

from core.admin.company_admin import CompanyRestrictedAdmin
from sales.models import DiscountPolicy, DiscountRule


class DiscountRuleInline(StackedInline):
    model = DiscountRule
    extra = 0
    hide_ordering_field = True
    ordering_field = "order"
    fieldsets = (
        (None, {
            "fields": (
                ("name", "is_active"),
                ("percentage", "mode", "level", "kind"),
                "condition",
            )
        }),
    )


@admin.register(DiscountPolicy)
class DiscountPolicyAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "is_active", "auto_apply", "company"]
    list_filter = ["is_active", "auto_apply", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    inlines = [DiscountRuleInline]
