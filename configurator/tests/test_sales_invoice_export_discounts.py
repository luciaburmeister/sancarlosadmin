"""
Tests for the discount percentages exposed to sales invoice export templates:
each line's own rate and the invoice-level effective rate.
"""
from decimal import Decimal

from django.test import TestCase

from company.models import Company
from configurator.services.export.sales_invoice import SalesInvoiceExportStrategy
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus
from sales.models import Client


class SalesInvoiceExportDiscountTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA,
        )

    def _add_item(self, unit_price, quantity, discount_percentage):
        SalesInvoiceItem.objects.create(
            invoice=self.invoice,
            description="Line",
            quantity=Decimal(str(quantity)),
            unit_price=Decimal(str(unit_price)),
            discount_percentage=Decimal(str(discount_percentage)),
        )
        self.invoice.recalculate_totals()

    def test_item_discount_percentage_is_exposed(self):
        self._add_item(unit_price="100.00", quantity="2", discount_percentage="10.00")

        item = SalesInvoiceExportStrategy(self.invoice).get_context()["items"][0]

        self.assertEqual(item["discount_percentage"], 10.00)
        self.assertEqual(item["discount_amount"], 20.00)
        self.assertEqual(item["line_total"], 180.00)

    def test_invoice_discount_percentage_derived_from_amount(self):
        self._add_item(unit_price="100.00", quantity="2", discount_percentage="0.00")
        self.invoice.discount_amount = Decimal("50.00")
        self.invoice.save()

        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        # "subtotal" is the taxable base printed on the document: lines
        # total net of the invoice-level discount.
        self.assertEqual(context["lines_total"], 200.00)
        self.assertEqual(context["gross_subtotal"], 200.00)
        self.assertEqual(context["subtotal"], 150.00)
        self.assertEqual(context["discount_amount"], 50.00)
        self.assertEqual(context["discount_percentage"], 25.00)
        self.assertEqual(
            context["subtotal"] + context["tax_amount"], context["total_amount"]
        )

    def test_no_discounts_report_zero(self):
        self._add_item(unit_price="100.00", quantity="1", discount_percentage="0.00")

        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        self.assertEqual(context["discount_percentage"], 0.0)
        self.assertEqual(context["items"][0]["discount_percentage"], 0.0)

    def test_empty_invoice_does_not_divide_by_zero(self):
        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        self.assertEqual(context["subtotal"], 0.0)
        self.assertEqual(context["discount_percentage"], 0.0)
