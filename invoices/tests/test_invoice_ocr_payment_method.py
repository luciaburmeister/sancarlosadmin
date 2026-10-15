"""
OCR-processed purchase invoices take the payment method configured on the
matched Provider, not whatever Gemini guessed from the document text.

The provider's method is the agreement accounting has with the vendor
(SEPA, confirming, ...) and the OCR prompt can only return transfer/cash/card,
so it must never overwrite the provider's configuration. The OCR reading is
only used when no provider could be linked.
"""
import tempfile

from django.test import TestCase, override_settings

from company.models import Company
from invoices.models import Invoice, PaymentMethod
from invoices.services.purchase_invoice_payment import resolve_purchase_invoice_payment_method
from invoices.tests.factories import OcrTestMixin
from materials.models import Provider

MEDIA_ROOT = tempfile.mkdtemp()


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
class InvoiceOcrPaymentMethodTests(OcrTestMixin, TestCase):
    ocr_defaults = {"payment_method": "card"}
    refresh_new_invoice = True

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Test Co", tax_identifier="A87654321")
        self.provider = Provider.objects.create(
            company=self.company,
            name="Proveedor SA",
            tax_identifier="B12345678",
            payment_method=PaymentMethod.SEPA_DIRECT_DEBIT,
        )

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
