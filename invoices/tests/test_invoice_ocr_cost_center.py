"""
OCR-processed purchase invoices default their cost center to the one
configured on the matched Provider.

Invoices created from the email inbox get it at creation through the model
hook (covered in test_cost_center_propagation); this file covers manually
uploaded invoices, which get it when the re-processing run links the provider
to the existing row. A cost center the user already set on the invoice is
never overwritten.

Run just this file:
    python manage.py test invoices.tests.test_invoice_ocr_cost_center
"""
import tempfile

from django.test import TestCase, override_settings

from accounting.models import CostCenter
from company.models import Company
from invoices.models import Invoice
from invoices.tests.factories import OcrTestMixin
from materials.models import Provider

MEDIA_ROOT = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class InvoiceOcrCostCenterTests(OcrTestMixin, TestCase):
    ocr_defaults = {"payment_method": "card"}

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Test Co", tax_identifier="A87654321")
        self.taller = CostCenter.objects.create(
            company=self.company, code="220", name="Taller"
        )
        self.admin = CostCenter.objects.create(
            company=self.company, code="110", name="Administracion"
        )
        self.provider = Provider.objects.create(
            company=self.company,
            name="Proveedor SA",
            tax_identifier="B12345678",
            default_cost_center=self.taller,
        )

    def test_uploaded_invoice_takes_provider_default_cost_center(self):
        invoice = Invoice.objects.create(company=self.company, invoice_number="F-001")
        self.assertIsNone(invoice.cost_center_id)

        invoice = self._process_existing(invoice)

        self.assertEqual(invoice.provider, self.provider)
        self.assertEqual(invoice.cost_center_id, self.taller.pk)

    def test_reprocess_never_overrides_a_hand_set_cost_center(self):
        invoice = Invoice.objects.create(
            company=self.company, invoice_number="F-001", cost_center=self.admin
        )

        invoice = self._process_existing(invoice)

        self.assertEqual(invoice.provider, self.provider)
        self.assertEqual(invoice.cost_center_id, self.admin.pk)

    def test_provider_without_default_leaves_it_empty(self):
        self.provider.default_cost_center = None
        self.provider.save()
        invoice = Invoice.objects.create(company=self.company, invoice_number="F-001")

        invoice = self._process_existing(invoice)

        self.assertEqual(invoice.provider, self.provider)
        self.assertIsNone(invoice.cost_center_id)

    def test_inactive_default_is_not_applied(self):
        self.taller.is_active = False
        self.taller.save()
        invoice = Invoice.objects.create(company=self.company, invoice_number="F-001")

        invoice = self._process_existing(invoice)

        self.assertIsNone(invoice.cost_center_id)
