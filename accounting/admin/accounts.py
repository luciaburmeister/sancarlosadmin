from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import CompanyAccount, ReportingLine, AccountReportingMap
from unfold.admin import TabularInline


class AccountReportingMapInlineUnfold(TabularInline):
    model = AccountReportingMap
    extra = 0
    autocomplete_fields = ["line"] # Needs line to have search_fields in its admin

@admin.register(CompanyAccount)
class CompanyAccountAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "company", "account_type", "parent", "role", "is_active", "is_postable", "ai_classifiable"]
    list_filter = ["company", "account_type", "is_active", "is_postable", "ai_classifiable", "is_reconcilable", "is_tax_account", "role"]
    search_fields = ["code", "name"]
    ordering = ["company", "code"]
    list_select_related = ["company", "parent"]
    autocomplete_fields = ["parent", "template_account","company"]
    
    inlines = [AccountReportingMapInlineUnfold]


@admin.register(ReportingLine)
class ReportingLineAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "company", "statement"]
    list_filter = ["company", "statement"]
    search_fields = ["code", "name"]
    ordering = ["company", "statement", "code"]
    list_select_related = ["company", "parent"]
    autocomplete_fields = ["parent","company"]


@admin.register(AccountReportingMap)
class AccountReportingMapAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["company", "account", "line", "weight"]
    list_filter = ["company", "line__statement"]
    search_fields = ["account__code", "line__code"]
    list_select_related = ["company", "account", "line"]
    autocomplete_fields = ["account", "line","company"]
