"""What a payment allocated to an invoice books: it settles the client's or provider's account."""
from django.utils.translation import gettext as _

from accounting.services.journal_generator import MissingAccount
from accounting.services.payment_journal import AllocationStrategy, CounterpartLine, register
from invoices.models import Invoice, SalesInvoice


@register
class SalesInvoiceStrategy(AllocationStrategy):
    model = SalesInvoice

    def lines(self, allocation, document, ledger):
        return [CounterpartLine(
            ledger.client_account(document.client),
            f"Factura {document.display_number} - {document.client.name}",
            allocation.amount_applied.amount,
        )]


@register
class PurchaseInvoiceStrategy(AllocationStrategy):
    model = Invoice

    def lines(self, allocation, document, ledger):
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
