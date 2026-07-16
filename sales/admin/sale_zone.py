from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from sales.models import SaleZone


@admin.register(SaleZone)
class SaleZoneAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "company"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
