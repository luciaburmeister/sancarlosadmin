from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from sales.models import CommissionistAgent


@admin.register(CommissionistAgent)
class CommissionistAgentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "tax_identifier", "commission_percentage", "release_policy", "is_active", "company"]
    list_filter = ["is_active", "company"]
    search_fields = ["name", "tax_identifier", "email"]
    autocomplete_fields = ["company"]
    fieldsets = (
        (_("Agent Information"), {
            "fields": (
                ("name", "company"),
                ("tax_identifier", "commission_percentage"),
                ("email", "phone"),
                ("release_policy", "is_active"),
            ),
        }),
    )
