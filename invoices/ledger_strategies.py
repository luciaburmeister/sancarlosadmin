"""
What a payment allocated to an invoice books: it settles the client's or
provider's account. Also an intercompany payment's two sides, see
invoices.models.IntercompanyPayment.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from accounting.services.journal_generator import MissingAccount
from accounting.models import JournalType
from accounting.services.payment_journal import (
    AllocationStrategy, CounterpartLine, Ledger, TreasuryStrategy, register, register_treasury,
)
from invoices.models import IntercompanyPayment, Invoice, SalesInvoice
from payments.models import PaymentMethod

if TYPE_CHECKING:
    from payments.models import Payment, PaymentAllocation


@register
class SalesInvoiceStrategy(AllocationStrategy[SalesInvoice]):
    model = SalesInvoice

    def lines(
        self, allocation: PaymentAllocation, document: SalesInvoice, ledger: Ledger
    ) -> list[CounterpartLine]:
        return [CounterpartLine(
            ledger.client_account(document.client),
            f"Factura {document.display_number} - {document.client.name}",
            allocation.amount_applied.amount,
        )]


@register
class PurchaseInvoiceStrategy(AllocationStrategy[Invoice]):
    model = Invoice

    def lines(
        self, allocation: PaymentAllocation, document: Invoice, ledger: Ledger
    ) -> list[CounterpartLine]:
        number = document.invoice_number or document.pk
        if not document.provider_id:
            raise MissingAccount(
                _("Purchase invoice %(number)s has no provider: set it so the payment can settle their account.")
                % {"number": number}
            )
        return [CounterpartLine(
            ledger.provider_account(document.provider),
            f"Factura {number} - {document.provider.name}",
            allocation.amount_applied.amount,
        )]


@register
class IntercompanyPaymentStrategy(AllocationStrategy[IntercompanyPayment]):
    """The payer's bank payment: what it paid for the debtor, the debtor owes it (430)."""

    model = IntercompanyPayment

    def lines(
        self, allocation: PaymentAllocation, document: IntercompanyPayment, ledger: Ledger
    ) -> list[CounterpartLine]:
        source = document.source_invoice
        return [CounterpartLine(
            ledger.client_account(document.payer_client),
            f"Pago por cuenta de {document.payer_client.name} - Fra {source.invoice_number or source.pk}",
            allocation.amount_applied.amount,
        )]


@register_treasury
class IntercompanySettlementTreasury(TreasuryStrategy):
    """The debtor's settlement: no money left it, the debt moved to the payer's account (400)."""

    method = PaymentMethod.INTERCOMPANY
    journal = (JournalType.GENERAL, "General", "GEN")

    def account(self, payment: Payment, ledger: Ledger):
        try:
            record = payment.intercompany_settlement
        except IntercompanyPayment.DoesNotExist:
            raise MissingAccount(
                _("Payment %(payment)s is marked as paid by another company but no intercompany payment "
                  "backs it.") % {"payment": payment.pk}
            )
        return ledger.provider_account(record.creditor_provider)
