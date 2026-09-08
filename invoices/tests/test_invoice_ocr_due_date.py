"""
Tests for the fallback due date on OCR-processed purchase invoices.

Gemini returns `due_date: null` whenever the document carries no vencimiento.
When that happens the invoice is due on its issue date — never "today", which
would silently make an invoice issued weeks ago look not-yet-overdue.
"""
import tempfile
from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from company.models import Company
from invoices.models import Invoice
from invoices.services.invoice.invoice_service import InvoiceService
from invoices.services.ocr_services import InvoiceData, OcrInterface
from materials.models import Provider

MEDIA_ROOT = tempfile.mkdtemp()


class FakeOcr(OcrInterface):
    """Returns a canned extraction instead of calling Gemini."""

    def __init__(self, **overrides):
        defaults = dict(
            document_type="invoice",
            vendor_name="Proveedor SA",
            vendor_tax_id="B12345678",
            company_tax_id="A87654321",
            invoice_number="F-001",
            date_issued="2026-01-15",
            due_date=None,
            net_amount=Decimal("100.00"),
            total_amount=Decimal("100.00"),
            currency="EUR",
            payment_method="transfer",
            line_items=[],
            taxes=[],
        )
        defaults.update(overrides)
        self.data = InvoiceData(**defaults)

    def extract_data(self, file_path: str) -> InvoiceData:
        return self.data


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class InvoiceOcrDueDateTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co", tax_identifier="A87654321")
        self.provider = Provider.objects.create(
            company=self.company, name="Proveedor SA", tax_identifier="B12345678",
        )

        self._file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._file.write(b"%PDF-1.4 fake invoice")
        self._file.close()
        self.file_path = self._file.name

    def _process_new(self, **ocr_overrides):
        """Pipeline A: OCR creates the invoice."""
        service = InvoiceService(ocr_processor=FakeOcr(**ocr_overrides))
        result = service.process_invoice(self.file_path)
        self.assertTrue(result.success, result.messages)
        return result.invoice

    def _process_existing(self, invoice, **ocr_overrides):
        """Pipeline B: OCR updates an invoice that already exists."""
        service = InvoiceService(ocr_processor=FakeOcr(**ocr_overrides))
        invoice.file.name = "invoices/uploads/existing.pdf"
        invoice.save(update_fields=["file"])
        result = service.process_existing_invoice(invoice, file_path=self.file_path)
        self.assertTrue(result.success, result.messages)
        result.invoice.refresh_from_db()
        return result.invoice

    # Pipeline A — invoice created by OCR

    def test_missing_due_date_falls_back_to_date_issued(self):
        invoice = self._process_new(due_date=None)

        self.assertEqual(invoice.date_issued, date(2026, 1, 15))
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

        today = timezone.now().date()
        self.assertEqual(invoice.date_issued, today)
        self.assertEqual(invoice.due_date, today)

    def test_fallback_due_date_is_persisted(self):
        invoice = self._process_new(due_date=None)
        invoice.refresh_from_db()

        self.assertEqual(invoice.due_date, date(2026, 1, 15))

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

        today = timezone.now().date()
        self.assertEqual(invoice.date_issued, today)
        self.assertEqual(invoice.due_date, today)
