"""
Tests for the fallback due date on OCR-processed purchase invoices.

Gemini returns `due_date: null` whenever the document carries no vencimiento.
When that happens the invoice is due on its issue date — never "today", which
would silently make an invoice issued weeks ago look not-yet-overdue.
"""
import tempfile
from datetime import date

from django.test import TestCase, override_settings
from django.utils import timezone

from company.models import Company
from invoices.models import Invoice
from invoices.tests.factories import OcrTestMixin
from materials.models import Provider

MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class InvoiceOcrDueDateTests(OcrTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Test Co", tax_identifier="A87654321")
        self.provider = Provider.objects.create(
            company=self.company, name="Proveedor SA", tax_identifier="B12345678",
        )

    # Pipeline A — invoice created by OCR

    def test_missing_due_date_falls_back_to_date_issued(self):
        invoice = self._process_new(due_date=None)

        self.assertEqual(invoice.date_issued, date(2026, 1, 15))
        self.assertEqual(invoice.due_date, date(2026, 1, 15))

        invoice.refresh_from_db()
        self.assertEqual(invoice.due_date, date(2026, 1, 15))

    def test_extracted_due_date_is_respected(self):
        invoice = self._process_new(due_date="2026-02-14")

        self.assertEqual(invoice.due_date, date(2026, 2, 14))

    def test_unparseable_due_date_falls_back_to_date_issued(self):
        """Gemini occasionally returns junk in a date field; that is a missing
        due date, not a reason to fall back to today."""
        invoice = self._process_new(due_date="n/a")

        self.assertEqual(invoice.due_date, date(2026, 1, 15))

    def test_no_dates_at_all_defaults_both_to_today(self):
        invoice = self._process_new(date_issued=None, due_date=None)

        today = timezone.localdate()
        self.assertEqual(invoice.date_issued, today)
        self.assertEqual(invoice.due_date, today)

    # Pipeline B — existing invoice reprocessed

    def test_reprocess_missing_due_date_falls_back_to_date_issued(self):
        invoice = Invoice.objects.create(
            company=self.company, provider=self.provider, invoice_number="F-001",
        )

        invoice = self._process_existing(invoice, due_date=None)

        self.assertEqual(invoice.date_issued, date(2026, 1, 15))
        self.assertEqual(invoice.due_date, date(2026, 1, 15))

    def test_reprocess_keeps_existing_due_date_when_ocr_finds_none(self):
        invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-001",
            date_issued=date(2026, 1, 15),
            due_date=date(2026, 3, 1),
        )

        invoice = self._process_existing(invoice, due_date=None)

        self.assertEqual(invoice.due_date, date(2026, 3, 1))

    def test_reprocess_extracted_due_date_wins_over_stored_one(self):
        invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-001",
            date_issued=date(2026, 1, 15),
            due_date=date(2026, 3, 1),
        )

        invoice = self._process_existing(invoice, due_date="2026-02-14")

        self.assertEqual(invoice.due_date, date(2026, 2, 14))

    def test_reprocess_with_no_dates_anywhere_defaults_both_to_today(self):
        invoice = Invoice.objects.create(
            company=self.company, provider=self.provider, invoice_number="F-001",
        )

        invoice = self._process_existing(invoice, date_issued=None, due_date=None)

        today = timezone.localdate()
        self.assertEqual(invoice.date_issued, today)
        self.assertEqual(invoice.due_date, today)
