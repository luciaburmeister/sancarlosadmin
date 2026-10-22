"""The ledger starts on the chart's ledger start date; the opening entry carries the balances from before."""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from accounting.models import (
    AccountRole, CompanyAccount, CompanyChartOfAccounts, EntryStatus, JournalEntry, JournalType,
)
from accounting.services.journal_generator import JournalGeneratorService, JournalizeError, MissingAccount
from accounting.services.opening_balance import OpeningBalanceService
from accounting.tests_payment_journal import PAID_ON, PaymentLedgerMixin
from invoices.models import SalesInvoiceStatus
from payments.models import PaymentDirection, PaymentStatus
from sales.models import Client

START = PAID_ON  # 2026-10-01
BEFORE = START - timedelta(days=10)


class LedgerStartMixin(PaymentLedgerMixin):
    def setUp(self):
        super().setUp()
        self.suspense = CompanyAccount.objects.create(
            company=self.company, code="555000", name="Partidas pendientes", role=AccountRole.SUSPENSE,
        )
        CompanyChartOfAccounts.objects.filter(company=self.company).update(ledger_start_date=START)

    def issued_on(self, day, *lines, client=None):
        """An issued sales invoice dated ``day``, journalized only if the ledger allows it."""
        invoice = self.proforma(*lines)
        if client:
            invoice.client = client
            invoice.save()
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        invoice.date_issued = day
        invoice.invoice_number = f"FV-{invoice.pk}"
        invoice.save(update_fields=["date_issued", "invoice_number"])
        return invoice

    def draft(self):
        return OpeningBalanceService(self.company).draft()


class LedgerStartTests(LedgerStartMixin, TestCase):
    def test_older_payments_stay_out_of_the_ledger(self):
        payment = self.pay("20.00", client=self.client_obj, payment_date=BEFORE)

        self.assertIsNone(self.sync(payment))
        self.assertFalse(payment.journal_entries.exists())

    def test_payments_from_the_start_date_are_booked(self):
        self.assertIsNotNone(self.sync(self.pay("20.00", client=self.client_obj)))

    def test_older_sales_invoices_are_not_journalized(self):
        from accounting.services.journal_generator import SalesJournalGeneratorService

        invoice = self.issued_on(BEFORE, "100.00")

        self.assertIsNone(SalesJournalGeneratorService(self.company).journalize_invoice(invoice))

    def test_older_purchase_invoices_are_not_journalized(self):
        old = self.purchase_invoice("50.00", date_issued=BEFORE)
        type(old).objects.filter(pk=old.pk).update(ocr_processed=True)

        self.assertIsNone(JournalGeneratorService(self.company).journalize_invoice(old))
        JournalGeneratorService(self.company).journalize_all_pending()
        self.assertFalse(old.journal_entries.exists())

    def test_start_date_is_fixed_once_the_opening_entry_is_posted(self):
        self.issued_on(BEFORE, "100.00")
        entry = self.draft()
        entry.post()
        chart = CompanyChartOfAccounts.objects.get(company=self.company)
        chart.ledger_start_date = START + timedelta(days=1)

        with self.assertRaises(ValidationError):
            chart.full_clean()


class OpeningEntryTests(LedgerStartMixin, TestCase):
    def test_balances_from_before_the_start_against_suspense(self):
        other = Client.objects.create(name="Otro SA", company=self.company)
        invoice = self.issued_on(BEFORE, "100.00")                     # 121 owed
        self.pay("50.00", (invoice, "50.00"), payment_date=BEFORE)     # 50 of it paid
        self.pay("30.00", client=other, payment_date=BEFORE)          # 30 on account
        self.purchase_invoice("200.00", date_issued=BEFORE)

        entry = self.draft()

        self.assertEqual(entry.status, EntryStatus.DRAFT)
        self.assertTrue(entry.is_opening)
        self.assertEqual(entry.date, START)
        self.assertEqual(entry.journal.type, JournalType.GENERAL)
        self.assertEqual(self.totals(entry), {
            "4000001": Decimal("-200.00"),
            "4300001": Decimal("71.00"),
            "4300002": Decimal("-30.00"),
            "555000": Decimal("159.00"),
        })

    def test_documents_from_the_start_date_are_left_to_the_ledger(self):
        self.issued_on(START, "100.00")
        self.pay("20.00", client=self.client_obj)

        self.assertFalse(self.draft().lines.exists())

    def test_old_invoice_paid_after_the_start_settles_the_opening_balance(self):
        invoice = self.issued_on(BEFORE, "100.00")
        opening = self.draft()
        payment = self.pay("121.00", (invoice, "121.00"))

        self.assertEqual(self.totals(opening)["4300001"], Decimal("121.00"))
        self.assertEqual(self.totals(self.sync(payment))["4300001"], Decimal("-121.00"))

    def test_proformas_and_cancelled_invoices_are_left_out(self):
        self.set_status(self.issued_on(BEFORE, "100.00"), SalesInvoiceStatus.CANCELLED)
        self.proforma("50.00")

        self.assertFalse(self.draft().lines.exists())

    def test_drafting_again_replaces_the_draft(self):
        first = self.draft()
        self.issued_on(BEFORE, "100.00")

        second = self.draft()

        self.assertFalse(JournalEntry.objects.filter(pk=first.pk).exists())
        self.assertEqual(self.totals(second)["4300001"], Decimal("121.00"))

    def test_a_posted_opening_entry_is_kept(self):
        self.issued_on(BEFORE, "100.00")
        self.draft().post()

        with translation.override("en"), self.assertRaisesMessage(JournalizeError, "already posted"):
            self.draft()

    def test_needs_the_start_date(self):
        CompanyChartOfAccounts.objects.filter(company=self.company).update(ledger_start_date=None)

        with self.assertRaises(JournalizeError):
            self.draft()

    def test_needs_the_suspense_account_when_something_is_pending(self):
        self.suspense.role = ""
        self.suspense.save()
        self.issued_on(BEFORE, "100.00")

        with self.assertRaisesMessage(MissingAccount, str(AccountRole.SUSPENSE.label)):
            self.draft()

    def test_a_payment_it_cannot_place_is_named(self):
        self.pay("20.00", payment_date=BEFORE, reference="SIN-CLIENTE")

        with self.assertRaisesMessage(JournalizeError, "SIN-CLIENTE"):
            self.draft()

    def test_outbound_payment_to_a_provider_lowers_what_is_owed(self):
        bill = self.purchase_invoice("200.00", date_issued=BEFORE)
        self.pay("80.00", (bill, "80.00"), direction=PaymentDirection.OUTBOUND, payment_date=BEFORE)

        self.assertEqual(self.totals(self.draft())["4000001"], Decimal("-120.00"))

    def test_draft_payments_do_not_count(self):
        self.pay("20.00", client=self.client_obj, payment_date=BEFORE, status=PaymentStatus.DRAFT)

        self.assertFalse(self.draft().lines.exists())


class OpeningEntryAdminTests(LedgerStartMixin, TestCase):
    def test_button_drafts_and_opens_the_entry(self):
        self.client.force_login(User.objects.create_superuser("finance", "fin@example.com", "pw"))
        self.issued_on(BEFORE, "100.00")
        chart = CompanyChartOfAccounts.objects.get(company=self.company)

        with translation.override("en"):
            response = self.client.get(
                reverse("admin:accounting_companychartofaccounts_draft_opening_entry_action", args=[chart.pk]),
            )
            entry = JournalEntry.objects.get(company=self.company, is_opening=True)
            self.assertRedirects(
                response, reverse("admin:accounting_journalentry_change", args=[entry.pk]),
                fetch_redirect_response=False,
            )
