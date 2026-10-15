"""
Tests for what sales invoice export templates receive about advance invoices:

- an advance invoice: its type, its percentage and its advance lines;
- an invoice deducting an advance: deduction lines flagged with their advance
  invoice and printed after the goods, and the header discount rate taken
  against the goods (the deduction added back), so the user's 2 % financing
  prints as 2.00 %, not 2.83 %.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from configurator.services.export.sales_invoice import SalesInvoiceExportStrategy
from invoices.models import SalesInvoiceDocumentType, SalesInvoiceItem
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin, UserExpedienteMixin


class UserExpedienteAdvanceExportTests(UserExpedienteMixin, TestCase):
    """The worked example: a 30 % advance, then "100 % del pendiente"."""

    def setUp(self):
        super().setUp()
        self.advance = self.issue_advance()

    def _rest(self):
        invoice = self.rest_invoice()
        invoice.refresh_from_db()
        return invoice

    def test_advance_invoice_context(self):
        context = SalesInvoiceExportStrategy(self.advance).get_context()

        self.assertTrue(context["is_advance"])
        self.assertEqual(context["advance_percentage"], 30.0)
        self.assertEqual(context["document_type_label"], self.advance.get_document_type_display())
        self.assertEqual(context["discount_percentage"], 0.0)
        self.assertEqual(context["advances_deducted"], 0.0)
        [item] = context["items"]
        self.assertTrue(item["is_advance_line"])
        self.assertFalse(item["is_advance_deduction"])
        self.assertIsNone(item["advance_invoice_number"])
        self.assertIsNone(item["advance_invoice_date"])
        self.assertEqual(item["line_total"], 457.90)

    def test_rest_invoice_prints_the_financing_discount_at_two_percent(self):
        rest = self._rest()

        context = SalesInvoiceExportStrategy(rest).get_context()

        # Lines 1,557.49 of goods less the 457.90 deduction; the 31.15
        # discount is 2 % of the goods, not of what the lines add up to.
        self.assertEqual(context["lines_total"], 1099.59)
        self.assertEqual(context["advances_deducted"], 457.90)
        self.assertEqual(context["gross_subtotal"], 1557.49)
        self.assertEqual(context["discount_amount"], 31.15)
        self.assertEqual(context["discount_percentage"], 2.00)
        self.assertEqual(context["subtotal"], 1068.44)
        self.assertEqual(context["total_amount"], 1292.81)
        self.assertFalse(context["is_advance"])
        self.assertIsNone(context["advance_percentage"])
        self.assertEqual(context["document_type_label"], rest.get_document_type_display())

    def test_rest_invoice_prints_the_deduction_last(self):
        rest = self._rest()

        items = SalesInvoiceExportStrategy(rest).get_context()["items"]

        *goods, deduction = items
        self.assertTrue(goods)
        self.assertEqual([item["id"] for item in goods], sorted(item["id"] for item in goods))
        for item in goods:
            self.assertFalse(item["is_advance_deduction"])
            self.assertFalse(item["is_advance_line"])
            self.assertIsNone(item["advance_invoice_number"])
        self.assertTrue(deduction["is_advance_deduction"])
        self.assertFalse(deduction["is_advance_line"])
        self.assertEqual(deduction["line_total"], -457.90)
        self.assertEqual(deduction["advance_invoice_number"], "INV-2026-00012")
        self.assertEqual(deduction["advance_invoice_date"], date(2026, 9, 3))


class DeductionOrderExportTests(AdvanceExpedienteMixin, TestCase):
    """Deductions print after the goods even when a goods line was added
    after them."""

    def test_deductions_print_after_goods_added_later(self):
        advance, advance_line = self.make_advance(Decimal("300.00"), "30")
        advance.invoice_number = "INV-2026-00001"
        advance.save(update_fields=["invoice_number"])
        invoice = self.invoice_goods()
        first_goods = list(invoice.items.order_by("pk").values_list("pk", flat=True))
        deduction = self.deduct(invoice, advance_line, Decimal("300.00"))
        extra = SalesInvoiceItem.objects.create(
            invoice=invoice, description="Extra", quantity=Decimal("1"),
            unit_price=Money(Decimal("50.00"), "EUR"),
        )

        items = SalesInvoiceExportStrategy(invoice).get_context()["items"]

        self.assertEqual([item["id"] for item in items], first_goods + [extra.pk, deduction.pk])
        self.assertEqual(
            [item["is_advance_deduction"] for item in items],
            [False] * (len(first_goods) + 1) + [True],
        )
        self.assertEqual(items[-1]["advance_invoice_number"], "INV-2026-00001")
        self.assertEqual(items[-1]["advance_invoice_date"], date(2026, 9, 1))

    def test_invoice_without_advances(self):
        invoice = self.invoice_goods()

        context = SalesInvoiceExportStrategy(invoice).get_context()

        self.assertEqual(context["advances_deducted"], 0.0)
        self.assertEqual(context["gross_subtotal"], context["lines_total"])
        self.assertFalse(context["is_advance"])
        self.assertIsNone(context["advance_percentage"])
        self.assertEqual(invoice.document_type, SalesInvoiceDocumentType.INVOICE)
        for item in context["items"]:
            self.assertFalse(item["is_advance_line"])
            self.assertFalse(item["is_advance_deduction"])
