"""
OCR-processed purchase invoices take the payment method configured on the
matched Provider, not whatever Gemini guessed from the document text.

The provider's method is the agreement accounting has with the vendor
(SEPA, confirming, ...) and the OCR prompt can only return transfer/cash/card,
so it must never overwrite the provider's configuration. The OCR reading is
only used when no provider could be linked.
"""
import tempfile
from decimal import Decimal

from django.test import TestCase, override_settings

from company.models import Company
from invoices.models import Invoice, PaymentMethod
from invoices.services.invoice.invoice_service import InvoiceService
from invoices.services.ocr_services import InvoiceData, OcrInterface
from invoices.services.purchase_invoice_payment import resolve_purchase_invoice_payment_method
from materials.models import Provider

MEDIA_ROOT = tempfile.mkdtemp()


class FakeOcr(OcrInterface):
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
            payment_method="card",
            line_items=[],
            taxes=[],
        )
        defaults.update(overrides)
        self.data = InvoiceData(**defaults)

    def extract_data(self, file_path: str) -> InvoiceData:
        return self.data


class ResolvePurchaseInvoicePaymentMethodTests(TestCase):
    def test_provider_method_wins_over_extracted(self):
        provider = Provider(payment_method=PaymentMethod.SEPA_DIRECT_DEBIT)
        self.assertEqual(
            resolve_purchase_invoice_payment_method(provider=provider, extracted="card"),
            PaymentMethod.SEPA_DIRECT_DEBIT,
        )

    def test_no_provider_uses_extracted(self):
        self.assertEqual(
            resolve_purchase_invoice_payment_method(provider=None, extracted="card"),
            PaymentMethod.CARD,
        )

    def test_no_provider_and_junk_extraction_keeps_current(self):
        self.assertEqual(
            resolve_purchase_invoice_payment_method(provider=None, extracted="paypal", current="cash"),
            PaymentMethod.CASH,
        )

    def test_nothing_known_defaults_to_transfer(self):
        self.assertEqual(
            resolve_purchase_invoice_payment_method(provider=None, extracted=None, current=None),
            PaymentMethod.TRANSFER,
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class InvoiceOcrPaymentMethodTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co", tax_identifier="A87654321")
        self.provider = Provider.objects.create(
            company=self.company,
            name="Proveedor SA",
            tax_identifier="B12345678",
            payment_method=PaymentMethod.SEPA_DIRECT_DEBIT,
        )

        self._file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        self._file.write(b"%PDF-1.4 fake invoice")
        self._file.close()
        self.file_path = self._file.name

    def _process_new(self, **ocr_overrides):
        service = InvoiceService(ocr_processor=FakeOcr(**ocr_overrides))
        result = service.process_invoice(self.file_path)
        self.assertTrue(result.success, result.messages)
        result.invoice.refresh_from_db()
        return result.invoice

    def _process_existing(self, invoice, **ocr_overrides):
        service = InvoiceService(ocr_processor=FakeOcr(**ocr_overrides))
        invoice.file.name = "invoices/uploads/existing.pdf"
        invoice.save(update_fields=["file"])
        result = service.process_existing_invoice(invoice, file_path=self.file_path)
        self.assertTrue(result.success, result.messages)
        result.invoice.refresh_from_db()
        return result.invoice

    # Pipeline A — invoice created by OCR (email inbox)

    def test_new_invoice_takes_provider_payment_method(self):
        invoice = self._process_new(payment_method="card")

        self.assertEqual(invoice.provider, self.provider)
        self.assertEqual(invoice.payment_method, PaymentMethod.SEPA_DIRECT_DEBIT)

    def test_new_invoice_without_provider_keeps_ocr_reading(self):
        invoice = self._process_new(vendor_tax_id=None, payment_method="card")

        self.assertIsNone(invoice.provider)
        self.assertEqual(invoice.payment_method, PaymentMethod.CARD)

    def test_new_invoice_without_provider_or_reading_defaults_to_transfer(self):
        invoice = self._process_new(vendor_tax_id=None, payment_method=None)

        self.assertEqual(invoice.payment_method, PaymentMethod.TRANSFER)

    # Pipeline B — manually uploaded invoice processed afterwards

    def test_uploaded_invoice_takes_provider_payment_method(self):
        invoice = Invoice.objects.create(company=self.company, invoice_number="F-001")

        invoice = self._process_existing(invoice, payment_method="card")

        self.assertEqual(invoice.provider, self.provider)
        self.assertEqual(invoice.payment_method, PaymentMethod.SEPA_DIRECT_DEBIT)

    def test_reprocess_overrides_previous_ocr_guess_with_provider_method(self):
        invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="F-001",
            payment_method=PaymentMethod.CASH,
        )

        invoice = self._process_existing(invoice, payment_method="card")

        self.assertEqual(invoice.payment_method, PaymentMethod.SEPA_DIRECT_DEBIT)

    def test_reprocess_without_provider_keeps_stored_method_when_ocr_finds_none(self):
        invoice = Invoice.objects.create(
            company=self.company, invoice_number="F-001", payment_method=PaymentMethod.CASH,
        )

        invoice = self._process_existing(invoice, vendor_tax_id=None, payment_method=None)

        self.assertIsNone(invoice.provider)
        self.assertEqual(invoice.payment_method, PaymentMethod.CASH)
