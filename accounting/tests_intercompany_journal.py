"""
An intercompany payment in both ledgers: the payer books what it paid as owed
by the debtor (430), the debtor moves its provider's debt to the payer (400),
and neither the debit note nor its mirror is ever journalized.
"""
from decimal import Decimal

from django.test import TestCase

from accounting.models import EntryStatus, JournalEntry, JournalType
from accounting.services.journal_generator import JournalGeneratorService, SalesJournalGeneratorService
from accounting.services.payment_journal.engine import PaymentJournalService
from invoices.services.intercompany_payment_service import IntercompanyPaymentService
from invoices.tests.factories import make_bank_account
from invoices.tests.intercompany_fixtures import IntercompanyMixin


class IntercompanyJournalTests(IntercompanyMixin, TestCase):
    def totals(self, payment):
        """{account: debit - credit} of the payment's live entry."""
        entry = payment.journal_entries.get(reversal_of__isnull=True, reversal__isnull=True)
        self.assertEqual(entry.status, EntryStatus.POSTED)
        totals = {}
        for line in entry.lines.select_related("account"):
            totals[line.account] = totals.get(line.account, 0) + line.debit.amount - line.credit.amount
        return entry, totals

    def test_the_payer_books_the_debtor_as_owing_it(self):
        record = self.register()

        entry, totals = self.totals(record.payer_payment)

        self.payer_bank.refresh_from_db()
        record.payer_client.refresh_from_db()
        self.assertEqual(entry.company, self.payer)
        self.assertEqual(entry.journal.type, JournalType.BANK)
        self.assertEqual(totals, {
            record.payer_client.account: Decimal("1210.00"),
            self.payer_bank.account: Decimal("-1210.00"),
        })
        self.assertTrue(record.payer_client.account.code.startswith("430"))

    def test_the_debtor_moves_the_debt_from_its_provider_to_the_payer(self):
        record = self.register()

        entry, totals = self.totals(record.settlement_payment)

        self.provider.refresh_from_db()
        record.creditor_provider.refresh_from_db()
        self.assertEqual(entry.company, self.debtor)
        self.assertEqual(entry.journal.type, JournalType.GENERAL)
        self.assertEqual(totals, {
            self.provider.account: Decimal("1210.00"),
            record.creditor_provider.account: Decimal("-1210.00"),
        })

    def test_debit_note_and_mirror_are_never_journalized(self):
        record = self.register()

        self.assertIsNone(SalesJournalGeneratorService(self.payer).journalize_invoice(record.debit_note))
        self.assertIsNone(JournalGeneratorService(self.debtor).journalize_invoice(record.mirror_invoice))
        JournalGeneratorService(self.debtor).journalize_all_pending()
        self.assertFalse(JournalEntry.objects.filter(sales_invoice=record.debit_note).exists())
        self.assertFalse(JournalEntry.objects.filter(purchase_invoice=record.mirror_invoice).exists())

    def test_once_reimbursed_nobody_owes_anybody(self):
        record = self.register()
        debtor_bank = make_bank_account(company=self.debtor, iban="ES0000000000000000001111")

        self.reimburse(record, debtor_bank)

        for partner in (record.payer_client, record.creditor_provider):
            partner.refresh_from_db()
            self.assertEqual(self.balance(partner.account), Decimal("0"))
        record.debit_note.refresh_from_db()
        record.mirror_invoice.refresh_from_db()
        self.assertEqual(record.debit_note.payment_status, "paid")
        self.assertEqual(record.mirror_invoice.payment_status, "paid")
        # The money only moved once each way: out of the payer, back to it
        self.payer_bank.refresh_from_db()
        self.assertEqual(self.balance(self.payer_bank.account), Decimal("0"))
        debtor_bank.refresh_from_db()
        self.assertEqual(self.balance(debtor_bank.account), Decimal("-1210.00"))

    def test_cancelling_reverses_both_entries(self):
        record = self.register()

        IntercompanyPaymentService.cancel(record, self.user)
        # What the commit runs. Not through captureOnCommitCallbacks: the
        # registering's callbacks stay queued in the test's transaction, and
        # schedule() skips a payment it finds already queued.
        for payment in (record.payer_payment, record.settlement_payment):
            PaymentJournalService.journalize_quietly(payment.pk)

        for payment in (record.payer_payment, record.settlement_payment):
            self.assertFalse(payment.journal_entries.filter(reversal_of__isnull=True, reversal__isnull=True).exists())
        record.payer_client.refresh_from_db()
        self.provider.refresh_from_db()
        self.assertEqual(self.balance(record.payer_client.account), Decimal("0"))
        self.assertEqual(self.balance(self.provider.account), Decimal("0"))
