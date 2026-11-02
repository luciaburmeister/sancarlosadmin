"""The generic purchase-invoice Excel import never reuses another company's provider."""
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.test import TestCase

from invoices.models import Invoice
from invoices.services.invoice.import_invoice_service import import_invoices_from_excel
from invoices.tests.factories import get_iva21, make_company, make_provider
from materials.models import Provider

HEADERS = ["invoice_number", "date_issued", "provider_cif", "provider_name", "base_amount",
           "iva_percentage", "iva_amount", "total", "company"]


def workbook(*rows):
    wb = openpyxl.Workbook()
    wb.active.append(HEADERS)
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


class PurchaseInvoiceImportProviderTest(TestCase):
    def setUp(self):
        get_iva21()
        self.ours = make_company(name="Ours")
        self.other = make_company(name="Other")

    def row(self, company="Ours"):
        return ["F-1", "01/10/2026", "B11111111", "Perfiles SA", 100, 21, 21, 121, company]

    def test_provider_of_another_company_is_not_reused(self):
        foreign = make_provider(company=self.other, tax_identifier="B11111111")

        result = import_invoices_from_excel(workbook(self.row()))

        self.assertEqual(result.created, 1)
        invoice = Invoice.objects.get(company=self.ours)
        self.assertNotEqual(invoice.provider, foreign)
        self.assertEqual(invoice.provider.company, self.ours)
        self.assertEqual(invoice.provider.tax_identifier, "B11111111")

    def test_provider_of_the_same_company_is_reused(self):
        own = make_provider(company=self.ours, tax_identifier="B11111111")

        import_invoices_from_excel(workbook(self.row()))

        self.assertEqual(Invoice.objects.get(company=self.ours).provider, own)
        self.assertEqual(Provider.objects.filter(tax_identifier="B11111111").count(), 1)

    def test_amounts_are_imported(self):
        import_invoices_from_excel(workbook(self.row()))

        self.assertEqual(Invoice.objects.get(company=self.ours).total_amount.amount, Decimal("121.00"))
