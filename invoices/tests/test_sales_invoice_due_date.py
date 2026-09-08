"""
Tests for the automatic due date on internal sales invoices.

An internal invoice created without a due date derives it from the client's
payment terms (`date_issued` + `payment_terms_days`) and snapshots those terms
on the invoice. External invoices keep whatever the imported document said,
and an explicit due date is always respected.
"""
from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from company.models import Company
from invoices.models import SalesInvoice
from sales.models import Client


class SalesInvoiceDueDateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_30 = Client.objects.create(
            name="Acme 30", company=self.company, payment_terms_days=30,
        )
        self.client_no_terms = Client.objects.create(
            name="Contado", company=self.company, payment_terms_days=0,
        )

    def _create(self, **kwargs):
        kwargs.setdefault("company", self.company)
        kwargs.setdefault("client", self.client_30)
        kwargs.setdefault("date_issued", date(2026, 1, 15))
        return SalesInvoice.objects.create(**kwargs)

    def test_due_date_derived_from_client_payment_terms(self):
        invoice = self._create()

        self.assertEqual(invoice.due_date, date(2026, 2, 14))
        self.assertEqual(invoice.payment_terms_days, 30)

    def test_derived_due_date_is_persisted(self):
        invoice = self._create()
        invoice.refresh_from_db()

        self.assertEqual(invoice.due_date, date(2026, 2, 14))
        self.assertEqual(invoice.payment_terms_days, 30)

    def test_explicit_due_date_is_respected(self):
        invoice = self._create(due_date=date(2026, 3, 1))

        self.assertEqual(invoice.due_date, date(2026, 3, 1))

    def test_invoice_own_payment_terms_win_over_client(self):
        invoice = self._create(payment_terms_days=60)

        self.assertEqual(invoice.due_date, date(2026, 3, 16))
        self.assertEqual(invoice.payment_terms_days, 60)

    def test_client_without_terms_is_due_on_issue_date(self):
        invoice = self._create(client=self.client_no_terms)

        self.assertEqual(invoice.due_date, date(2026, 1, 15))
        self.assertEqual(invoice.payment_terms_days, 0)

    def test_external_invoice_keeps_empty_due_date(self):
        invoice = self._create(is_external=True)

        self.assertIsNone(invoice.due_date)
        self.assertEqual(invoice.payment_terms_days, 0)

    def test_external_invoice_keeps_its_own_due_date(self):
        invoice = self._create(is_external=True, due_date=date(2026, 4, 30))

        self.assertEqual(invoice.due_date, date(2026, 4, 30))

    def test_default_date_issued_yields_a_date_not_a_datetime(self):
        """`date_issued` defaults to `timezone.now`, so the arithmetic must
        still produce a plain date."""
        invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_30,
        )

        self.assertEqual(invoice.due_date, timezone.localdate() + timedelta(days=30))
        self.assertIsInstance(invoice.due_date, date)

    def test_existing_invoice_keeps_cleared_due_date(self):
        """Auto-fill happens at creation only; clearing it later is a
        deliberate edit and must not be silently undone."""
        invoice = self._create()
        invoice.due_date = None
        invoice.save()
        invoice.refresh_from_db()

        self.assertIsNone(invoice.due_date)
