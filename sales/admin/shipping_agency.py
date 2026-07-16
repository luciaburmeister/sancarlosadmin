from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from sales.models import ShippingAgency


@admin.register(ShippingAgency)
class ShippingAgencyAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "phone", "email", "website", "is_active", "company"]
    list_filter = ["is_active"]
    search_fields = ["name", "email", "phone"]
    autocomplete_fields = ["company"]
