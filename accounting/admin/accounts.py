from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import CompanyAccount, ReportingLine, AccountReportingMap

class AccountReportingMapInline(admin.TabularInline):
    model = AccountReportingMap
    extra = 0
    autocomplete_fields = ["line"]
    verbose_name = "Reporting Mapping"
    verbose_name_plural = "Reporting Mappings"
    # Unfold TabularInline?
    # I should check if Unfold has TabularInline. Yes it does.
    # from unfold.admin import TabularInline

from unfold.admin import TabularInline

class AccountReportingMapInlineUnfold(TabularInline):
    model = AccountReportingMap
    extra = 0
    autocomplete_fields = ["line"] # Needs line to have search_fields in its admin

@admin.register(CompanyAccount)
class CompanyAccountAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "company", "account_type", "parent", "is_active", "is_postable","company"]
    list_filter = ["company", "account_type", "is_active", "is_postable", "is_reconcilable", "is_tax_account"]
    search_fields = ["code", "name"]
    ordering = ["company", "code"]
    list_select_related = ["company", "parent"]
    autocomplete_fields = ["parent", "template_account"]
    
    inlines = [AccountReportingMapInlineUnfold]


@admin.register(ReportingLine)
class ReportingLineAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "company", "statement"]
    list_filter = ["company", "statement"]
    search_fields = ["code", "name"]
    ordering = ["company", "statement", "code"]
    list_select_related = ["company", "parent"]


@admin.register(AccountReportingMap)
class AccountReportingMapAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["company", "account", "line", "weight"]
    list_filter = ["company", "line__statement"]
    search_fields = ["account__code", "line__code"]
    list_select_related = ["company", "account", "line"]
    autocomplete_fields = ["account", "line"]
