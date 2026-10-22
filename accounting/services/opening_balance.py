"""
The opening entry (asiento de apertura) of a company whose ledger starts on
the chart's ledger start date.
"""
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext as _

from accounting.models import AccountRole, EntryStatus, JournalEntry, JournalType
from accounting.services.journal_generator import LEDGER_CURRENCY, JournalizeError, _JournalGeneratorBase
from accounting.services.payment_journal.engine import BOOKED_STATUSES, PaymentJournalService
from invoices.models import Invoice, SalesInvoice, SalesInvoiceStatus
from payments.models import Payment


class OpeningBalanceService(_JournalGeneratorBase):
    """
    Each client's and provider's balance on the ledger start date, as the
    journal generators would have booked everything dated earlier: issued
    sales invoices on the client, purchase invoices on the provider, and the
    payments made before then settling them (unallocated money on the payer,
    as PaymentJournalService does). Payments allocated to other documents
    (loans, taxes, expenses) leave those allocations out.

    What the balances don't add up to goes to the SUSPENSE role account. The
    entry is only drafted: the accountant replaces that line with the bank,
    equity and other balances, then posts it.
    """

    def draft(self):
        """The opening entry, drafted again from the documents as they are now. Raises JournalizeError."""
        chart = self._chart()
        start = chart.ledger_start_date if chart else None
        if not start:
            raise JournalizeError(_("Set the ledger start date on the chart of accounts first."))

        with transaction.atomic():
            existing = JournalEntry.objects.select_for_update().filter(company=self.company, is_opening=True).first()
            if existing and existing.status != EntryStatus.DRAFT:
                raise JournalizeError(_("The opening entry is already posted: reverse it to draft it again."))

            amounts = self._balances(start)
            pending = -sum(amount for _account, _description, amount in amounts)
            if pending:
                amounts.append((self._role_account(AccountRole.SUSPENSE), "Saldos pendientes de apertura", pending))
            if existing:
                existing.delete()
            entry = JournalEntry.objects.create(
                company=self.company,
                journal=self._get_journal(JournalType.GENERAL, "General", "GEN"),
                date=start,
                reference="Asiento de apertura",
                status=EntryStatus.DRAFT,
                is_opening=True,
            )
            self._create_lines(entry, amounts, LEDGER_CURRENCY)
        return entry

    def _balances(self, start):
        """[(partner account, description, balance)], debit positive, by account code."""
        payments = PaymentJournalService(self.company)
        totals = defaultdict(Decimal)

        for invoice in SalesInvoice.objects.filter(
            company=self.company, status=SalesInvoiceStatus.FINAL, date_issued__lt=start,
        ).select_related("client"):
            self._check_currency(invoice.currency)
            totals[payments.client_account(invoice.client)] += Decimal(str(invoice.total_amount.amount))

        for invoice in Invoice.objects.filter(
            company=self.company, date_issued__lt=start, provider__isnull=False,
        ).select_related("provider"):
            self._check_currency(invoice.currency)
            if invoice.total_amount:
                totals[payments.provider_account(invoice.provider)] -= Decimal(str(invoice.total_amount.amount))

        for payment in Payment.objects.filter(
            company=self.company, status__in=BOOKED_STATUSES, payment_date__lt=start,
        ).exclude(amount=0).select_related("client", "provider"):
            try:
                self._check_currency(payment.amount_currency)
                counterparts = payments.counterpart_amounts(payment, only=(SalesInvoice, Invoice))
            except JournalizeError as e:
                raise JournalizeError(
                    _("Payment %(payment)s: %(error)s") % {"payment": payment.reference or payment.pk, "error": e}
                ) from e
            for account, _description, amount in counterparts:
                totals[account] += amount

        return [
            (account, f"Saldo de apertura - {account.name}", amount)
            for account, amount in sorted(totals.items(), key=lambda item: item[0].code)
            if amount
        ]
