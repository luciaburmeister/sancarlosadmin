"""
Tests for currency handling on purchase invoices.

Verifies:
1. Normalization of currency strings (USD, symbols, bracketed codes).
2. InvoiceService correctly stores USD when extracted by OCR.
3. Money fields on Invoice, InvoiceItem, and AppliedTax preserve USD and do not default to EUR.
4. recalculate_totals preserves the invoice's currency.
"""
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from djmoney.money import Money

from company.models import Company
from invoices.models import Invoice, InvoiceItem, AppliedTax, TaxType
from invoices.services.invoice.invoice_service import InvoiceService
from invoices.services.ocr_services import InvoiceData, OcrInterface, LineItem, TaxItem
from invoices.services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
from materials.models import Provider

MEDIA_ROOT = tempfile.mkdtemp()


class FakeOcr(OcrInterface):
    def __init__(self, **overrides):
        defaults = dict(
            document_type="invoice",
            vendor_name="Jinan Bcamcnc Machinery",
            vendor_tax_id="CHINA9999",
            company_tax_id="B22681316",
            invoice_number="BCWMJ260224",
            date_issued="2026-08-06",
            due_date="2026-08-06",
            net_amount=Decimal("27900.00"),
            total_amount=Decimal("27900.00"),
            currency="USD",
            payment_method="transfer",
            line_items=[
                LineItem(
                    description="CNC ROUTER 2150C",
                    quantity=Decimal("1"),
                    unit_price=Decimal("22700.00"),
                    discount_percentage=Decimal("0"),
                    gross_total=Decimal("22700.00"),
                    net_total=Decimal("22700.00"),
                    tax_rate=Decimal("0"),
                ),
                LineItem(
                    description="BCJ1325",
                    quantity=Decimal("1"),
                    unit_price=Decimal("5200.00"),
                    discount_percentage=Decimal("0"),
                    gross_total=Decimal("5200.00"),
                    net_total=Decimal("5200.00"),
                    tax_rate=Decimal("0"),
                ),
            ],
            taxes=[],
        )
        defaults.update(overrides)
        self.data = InvoiceData(**defaults)

    def extract_data(self, file_path: str) -> InvoiceData:
        return self.data


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class InvoiceCurrencyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Fistex Energy", tax_identifier="B22681316")
        self.provider = Provider.objects.create(
            company=self.company, name="Jinan Bcamcnc Machinery", tax_identifier="CHINA9999"
        )
        self._file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._file.write(b"%PDF-1.4 test invoice")
        self._file.close()

    def test_normalize_currency(self):
        processor = GeminiInvoiceProcessor(api_key="fake")
        self.assertEqual(processor._normalize_currency("USD"), "USD")
        self.assertEqual(processor._normalize_currency("(USD)"), "USD")
        self.assertEqual(processor._normalize_currency("TOTAL AMOUNT (USD)"), "USD")
        self.assertEqual(processor._normalize_currency("$"), "USD")
        self.assertEqual(processor._normalize_currency("USD $"), "USD")
        self.assertEqual(processor._normalize_currency("€"), "EUR")
        self.assertEqual(processor._normalize_currency("EUR"), "EUR")
        self.assertEqual(processor._normalize_currency("DOLLARS"), "USD")
        self.assertIsNone(processor._normalize_currency(None))

    def test_invoice_creation_stores_usd(self):
        service = InvoiceService(ocr_processor=FakeOcr())
        result = service.process_invoice(self._file.name)
        self.assertTrue(result.success)
        invoice = result.invoice

        # Verify Invoice currency fields
        self.assertEqual(invoice.currency, "USD")
        self.assertEqual(str(invoice.net_amount.currency), "USD")
        self.assertEqual(str(invoice.total_amount.currency), "USD")
        self.assertEqual(invoice.net_amount_currency, "USD")
        self.assertEqual(invoice.total_amount_currency, "USD")

        # Verify items
        items = list(invoice.items.all())
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(str(item.unit_price.currency), "USD")
            self.assertEqual(str(item.gross_total.currency), "USD")
            self.assertEqual(str(item.discount_amount.currency), "USD")
            self.assertEqual(str(item.line_total.currency), "USD")

        # Verify recalculate_totals preserves USD
        invoice.recalculate_totals(save=True)
        invoice.refresh_from_db()
        self.assertEqual(str(invoice.total_amount.currency), "USD")
        self.assertEqual(invoice.total_amount_currency, "USD")
        self.assertEqual(invoice.total_amount.amount, Decimal("27900.00"))

    def test_process_existing_invoice_stores_usd(self):
        # Create an existing invoice with default EUR
        invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="TEMP_001",
            file=SimpleUploadedFile("inv.pdf", b"%PDF-1.4"),
        )
        self.assertEqual(invoice.currency, "EUR")

        service = InvoiceService(ocr_processor=FakeOcr())
        result = service.process_existing_invoice(invoice, file_path=self._file.name)
        self.assertTrue(result.success)

        invoice.refresh_from_db()
        self.assertEqual(invoice.currency, "USD")
        self.assertEqual(str(invoice.net_amount.currency), "USD")
        self.assertEqual(str(invoice.total_amount.currency), "USD")
        self.assertEqual(invoice.net_amount_currency, "USD")
        self.assertEqual(invoice.total_amount_currency, "USD")

        for item in invoice.items.all():
            self.assertEqual(str(item.unit_price.currency), "USD")
            self.assertEqual(str(item.line_total.currency), "USD")

    def test_invoice_with_taxes_in_usd(self):
        ocr = FakeOcr(
            net_amount=Decimal("100.00"),
            total_amount=Decimal("121.00"),
            currency="USD",
            taxes=[
                TaxItem(
                    name="IVA",
                    percentage=Decimal("21"),
                    base_amount=Decimal("100.00"),
                    tax_amount=Decimal("21.00"),
                )
            ]
        )
        service = InvoiceService(ocr_processor=ocr)
        result = service.process_invoice(self._file.name)
        self.assertTrue(result.success)
        invoice = result.invoice

        taxes = list(invoice.get_taxes())
        self.assertEqual(len(taxes), 1)
        tax = taxes[0]
        self.assertEqual(str(tax.base_amount.currency), "USD")
        self.assertEqual(str(tax.calculated_amount.currency), "USD")
