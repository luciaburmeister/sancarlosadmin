from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import CostCenter


@admin.register(CostCenter)
class CostCenterAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "hierarchy", "parent", "is_active", "is_assignable", "company"]
    list_filter = ["company", "is_active", "is_assignable"]
    # Required for the autocomplete widgets on Provider, PurchaseOrder and Invoice.
    search_fields = ["code", "name"]
    ordering = ["company", "code"]
    list_select_related = ["company", "parent"]
    autocomplete_fields = ["parent", "company"]

    fieldsets = (
        (None, {
            "fields": (
                ("code", "name"),
                "parent",
                ("is_active", "is_assignable"),
                "company",
            )
        }),
    )

    @display(description=_("Name"), ordering="name")
    def hierarchy(self, obj):
        return obj.full_name
