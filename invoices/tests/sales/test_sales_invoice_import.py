"""The sales-invoice Excel import never reuses another company's client."""
from io import BytesIO

import openpyxl
from django.test import TestCase

from invoices.models import SalesInvoice
from invoices.services.import_sales_invoice_service import import_sales_invoices_from_excel
from invoices.tests.factories import get_iva21, make_company
from sales.models import Client

HEADERS = ["invoice_number", "date_issued", "company", "client_cif", "client_name", "base_amount",
           "iva_percentage", "iva_amount", "total"]


def workbook(*rows):
    wb = openpyxl.Workbook()
    wb.active.append(HEADERS)
    for row in rows:
        wb.active.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


class SalesInvoiceImportClientTest(TestCase):
    def setUp(self):
        get_iva21()
        self.ours = make_company(name="Ours")
        self.other = make_company(name="Other")
        self.row = ["V-1", "01/10/2026", "Ours", "B22222222", "Acme", 100, 21, 21, 121]

    def test_client_of_another_company_is_not_reused(self):
        foreign = Client.objects.create(name="Acme", company=self.other, tax_identifier="B22222222")

        result = import_sales_invoices_from_excel(workbook(self.row))

        self.assertEqual(result.created, 1)
        invoice = SalesInvoice.objects.get(company=self.ours)
        self.assertNotEqual(invoice.client, foreign)
        self.assertEqual(invoice.client.company, self.ours)

    def test_client_of_the_same_company_is_reused(self):
        own = Client.objects.create(name="Acme", company=self.ours, tax_identifier="B22222222")

        import_sales_invoices_from_excel(workbook(self.row))

        self.assertEqual(SalesInvoice.objects.get(company=self.ours).client, own)
        self.assertEqual(Client.objects.filter(tax_identifier="B22222222").count(), 1)
