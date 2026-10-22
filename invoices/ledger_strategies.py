"""What a payment allocated to an invoice books: it settles the client's or provider's account."""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from accounting.services.journal_generator import MissingAccount
from accounting.services.payment_journal import AllocationStrategy, CounterpartLine, Ledger, register
from invoices.models import Invoice, SalesInvoice

if TYPE_CHECKING:
    from payments.models import PaymentAllocation


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
