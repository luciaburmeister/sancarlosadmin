from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import BusinessUnit


@admin.register(BusinessUnit)
class BusinessUnitAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "income_type", "is_default", "is_active", "company"]
    list_filter = ["company", "income_type", "is_default", "is_active"]
    search_fields = ["code", "name"]
    ordering = ["company", "code"]
    list_select_related = ["company"]
    autocomplete_fields = ["company"]
    readonly_fields = ["code"]

    fieldsets = (
        (None, {
            "fields": (
                ("code", "name"),
                "income_type",
                ("is_default", "is_active"),
                "company",
            )
        }),
    )
