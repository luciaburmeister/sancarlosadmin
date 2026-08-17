from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from ..models import TransportCompatibilityRule


@admin.register(TransportCompatibilityRule)
class TransportCompatibilityRuleAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "is_active", "order", "condition_expression")
    list_filter = ("is_active",)
    search_fields = ("name", "condition_expression")
    ordering = ("order", "id")
