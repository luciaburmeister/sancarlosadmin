from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
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
    list_display = ["company", "template", "ledger_start_date", "lock_date"]
    list_filter = ["company", "template"]
    search_fields = ["company__name", "template__name"]
    list_select_related = ["company", "template"]
    autocomplete_fields = ["company","template"]

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "company",
                    "template",
                    "ledger_start_date",
                    "lock_date",
                    ("provider_account_root", "client_account_root", "bank_account_root"),
                    "partner_account_length",
                    "rounding_tolerance",
                )
            },
        ),
    )
    actions_detail = ["draft_opening_entry_action"]

    @action(description=_("Draft Opening Entry"), icon="start", permissions=["change"])
    def draft_opening_entry_action(self, request, object_id):
        from accounting.services.journal_generator import JournalizeError
        from accounting.services.opening_balance import OpeningBalanceService

        chart = get_object_or_404(self.get_queryset(request), pk=object_id)
        try:
            entry = OpeningBalanceService(chart.company).draft()
        except JournalizeError as e:
            self.message_user(request, str(e), level=messages.ERROR)
            return redirect(reverse("admin:accounting_companychartofaccounts_change", args=[object_id]))
        self.message_user(
            request,
            _("Opening entry drafted: replace the suspense line with the bank, equity and other balances, then post it."),
        )
        return redirect(reverse("admin:accounting_journalentry_change", args=[entry.pk]))


@admin.register(Journal)
class JournalAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["code", "name", "company", "type"]
    list_filter = ["company", "type"]
    search_fields = ["code", "name"]
    ordering = ["company", "code"]
    list_select_related = ["company"]
