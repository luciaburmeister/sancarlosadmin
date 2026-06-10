"""Banking admin — BankTransaction and BankReconciliation."""
import logging

from django.contrib import admin
from django.urls import path
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import display

from banking.models import BankReconciliation, BankTransaction, ReconciliationStatus
from banking.views import (
    ConnectBankView,
    ConsentDoneView,
    InitiateConsentView,
    SyncAccountsView,
)

logger = logging.getLogger(__name__)


@admin.register(BankTransaction)
class BankTransactionAdmin(ModelAdmin):
    list_display = (
        "id",
        "transaction_date",
        "display_amount",
        "counterparty_name",
        "description_short",
        "get_reconciliation_label",
        "bank_account",
    )
    list_filter = (
        "reconciliation_status",
        "bank_account",
        "transaction_date",
    )
    search_fields = (
        "description",
        "counterparty_name",
        "counterparty_iban",
        "external_id",
    )
    readonly_fields = (
        "external_id",
        "raw_data",
        "imported_at",
    )
    list_select_related = ("bank_account",)
    ordering = ("-transaction_date", "-imported_at")
    list_per_page = 30

    def get_urls(self):
        custom_urls = [
            path(
                "connect/",
                self.admin_site.admin_view(
                    ConnectBankView.as_view(model_admin=self)
                ),
                name="banking_connect_bank",
            ),
            path(
                "connect/initiate/",
                self.admin_site.admin_view(
                    InitiateConsentView.as_view(model_admin=self)
                ),
                name="banking_initiate_consent",
            ),
            path(
                "connect/done/",
                self.admin_site.admin_view(
                    ConsentDoneView.as_view(model_admin=self)
                ),
                name="banking_consent_done",
            ),
            path(
                "sync/",
                self.admin_site.admin_view(
                    SyncAccountsView.as_view(model_admin=self)
                ),
                name="banking_sync",
            ),
        ]
        return custom_urls + super().get_urls()

    @admin.display(description=_("Amount"))
    def display_amount(self, obj):
        return obj.amount

    @admin.display(description=_("Description"))
    def description_short(self, obj):
        return (obj.description or "")[:80]

    @display(
        description=_("Reconciliation"),
        ordering="reconciliation_status",
        label={
            ReconciliationStatus.UNRECONCILED: "warning",
            ReconciliationStatus.PARTIAL: "info",
            ReconciliationStatus.RECONCILED: "success",
        },
    )
    def get_reconciliation_label(self, obj):
        return obj.reconciliation_status


@admin.register(BankReconciliation)
class BankReconciliationAdmin(ModelAdmin):
    list_display = (
        "id",
        "bank_transaction",
        "payment",
        "get_confirmed_label",
        "method",
        "reconciled_at",
        "confirmed_at",
    )
    list_filter = (
        "is_confirmed",
        "method",
    )
    search_fields = (
        "bank_transaction__description",
        "bank_transaction__counterparty_name",
    )
    readonly_fields = (
        "suggested_allocations",
        "reconciled_at",
    )
    list_select_related = ("bank_transaction", "payment")
    ordering = ("-reconciled_at",)
    list_per_page = 30

    @display(
        description=_("Confirmed"),
        boolean=True,
    )
    def get_confirmed_label(self, obj):
        return obj.is_confirmed
