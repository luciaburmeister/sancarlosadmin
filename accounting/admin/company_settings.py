from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import FiscalYear, CompanyChartOfAccounts, Journal, JournalType

@admin.register(FiscalYear)
class FiscalYearAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "company", "start_date", "end_date", "is_closed"]
    list_filter = ["company", "is_closed"] # CompanyRestrictedAdmin handles filtering but we might want explicit filter for SU
    search_fields = ["name"]
    ordering = ["-end_date"]
    list_select_related = ["company"]


@admin.register(CompanyChartOfAccounts)
class CompanyChartOfAccountsAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["company", "template", "lock_date"]
    list_filter = ["company", "template"]
    search_fields = ["company__name", "template__name"]
    list_select_related = ["company", "template"]


@admin.register(Journal)
class JournalAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "company", "type"]
    list_filter = ["company", "type"]
    search_fields = ["code", "name"]
    ordering = ["company", "code"]
    list_select_related = ["company"]
