"""
Tests for the quote number/date exposed per line in the sales invoice
export context (Quote#id doubles as Fismec's delivery-note number).
"""
from datetime import date
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from company.models import Company
from configurator.services.export.sales_invoice import SalesInvoiceExportStrategy
from invoices import line_sources
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus
from materials.models import Category, Material
from sales.models import Client, Quote, QuoteItem


class SalesInvoiceExportQuoteDateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        category = Category.objects.create(name="Profiles", company=cls.company)
        cls.material = Material.objects.create(
            reference_code="MAT-0",
            name="Profile",
            category=category,
            cost_per_unit=Decimal("0"),
        )
        cls.quote = Quote.objects.create(
            company=cls.company, client=cls.client_obj, date=date(2026, 1, 15),
        )

    def _make_invoice_with_items(self, count):
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA,
        )
        for _ in range(count):
            quote_item = QuoteItem.objects.create(
                quote=self.quote,
                material=self.material,
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
            )
            SalesInvoiceItem.objects.create(
                invoice=invoice,
                description="Line",
                quantity=Decimal("1"),
                unit_price=Decimal("10.00"),
                source_id=line_sources.source_id(line_sources.QUOTE_ITEM, quote_item.pk),
            )
        invoice.recalculate_totals()
        return invoice

    def test_quote_id_and_date_are_exposed_per_line(self):
        invoice = self._make_invoice_with_items(count=1)

        item = SalesInvoiceExportStrategy(invoice).get_context()["items"][0]

        self.assertEqual(item["quote_id"], self.quote.pk)
        self.assertEqual(item["quote_date"], self.quote.date)

    def test_lines_with_no_quote_get_no_quote_date(self):
        invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj, status=SalesInvoiceStatus.PROFORMA,
        )
        SalesInvoiceItem.objects.create(
            invoice=invoice, description="Shop line", quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
        )
        invoice.recalculate_totals()

        item = SalesInvoiceExportStrategy(invoice).get_context()["items"][0]

        self.assertIsNone(item["quote_id"])
        self.assertIsNone(item["quote_date"])

    def test_quote_resolution_does_not_scale_with_item_count(self):
        single_item_invoice = self._make_invoice_with_items(count=1)
        many_items_invoice = self._make_invoice_with_items(count=4)

        with CaptureQueriesContext(connection) as single:
            SalesInvoiceExportStrategy(single_item_invoice).get_context()
        with CaptureQueriesContext(connection) as many:
            SalesInvoiceExportStrategy(many_items_invoice).get_context()

        self.assertEqual(len(single.captured_queries), len(many.captured_queries))
