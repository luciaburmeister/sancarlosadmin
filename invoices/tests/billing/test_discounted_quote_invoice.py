"""
Tests for invoicing project files whose quotes carry a discount.

The quote's tax is computed on the discounted subtotal (the "taxable base"),
so invoices created from the project file must do the same — otherwise a
100% invoice of a discounted quote overshoots the quote total and trips the
over-invoicing validation.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from invoices.models import AppliedTax
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.tests.factories import ExpedienteMixin, make_quote
from sales.models import Quote


class _DiscountedQuoteTestBase(ExpedienteMixin, TestCase):
    """A project file with one accepted quote carrying a 198.67 € discount.

    Numbers mirror a real case: subtotal 1045.62, discount 198.67,
    taxable base 846.95, IVA 21% = 177.86, total 1024.81.
    """

    SUBTOTAL = Decimal("1045.62")
    DISCOUNT = Decimal("198.67")
    TAXABLE = Decimal("846.95")
    TAX = Decimal("177.86")     # 21% of 846.95
    TOTAL = Decimal("1024.81")  # 846.95 + 177.86
    # Legacy breakdown shape: no "kind"/"level" keys.
    DISCOUNT_BREAKDOWN = [
        {"base": "1045.62", "mode": "net", "amount": "104.56",
         "rule_name": "10%", "percentage": "10.00"},
        {"base": "941.06", "mode": "accumulated", "amount": "94.11",
         "rule_name": "10% si mayor a 500", "percentage": "10.00"},
    ]

    project_file_name = "PF Discount"
    invoice_date_issued = date(2026, 7, 14)
    invoice_due_date = date(2026, 8, 14)

    def setUp(self):
        super().setUp()
        self.quote = make_quote(
            project_file=self.project_file,
            project_name="Discounted Quote",
            subtotal_price=self.SUBTOTAL,
            discount_amount=self.DISCOUNT,
            discounted_subtotal=self.TAXABLE,
            tax_amount=self.TAX,
            total_price=self.TOTAL,
            discount_breakdown=self.DISCOUNT_BREAKDOWN,
        )
        self.config = self._make_config(self.quote, self.SUBTOTAL, name="Corredera")

    def _full_items(self):
        """Line items for 100% of the quote (pre-discount, as the wizard builds them)."""
        return [{
            "source_id": f"config_{self.config.pk}",
            "description": "Corredera (Q#%d)" % self.quote.pk,
            "quantity": Decimal("1"),
            "unit_price": self.SUBTOTAL,
            "discount_percentage": Decimal("0.00"),
        }]


class FullInvoiceOfDiscountedQuoteTests(_DiscountedQuoteTestBase):

    def test_full_invoice_of_legacy_breakdown_quote(self):
        """100% of a discounted quote (legacy breakdown, no kind/level keys)
        does not trip the over-invoicing check, carries the discount and
        breakdown, taxes the discounted base and leaves nothing pending."""
        invoice = self._create_invoice(self._full_items())
        self.assertIsNotNone(invoice.pk)

        self.assertEqual(invoice.subtotal_amount.amount, self.SUBTOTAL)
        self.assertEqual(invoice.discount_amount.amount, self.DISCOUNT)
        self.assertEqual(invoice.tax_amount.amount, self.TAX)
        self.assertEqual(invoice.total_amount.amount, self.TOTAL)

        applied = AppliedTax.objects.get(
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
        )
        self.assertEqual(applied.base_amount.amount, self.TAXABLE)
        self.assertEqual(applied.calculated_amount.amount, self.TAX)
        self.assertEqual(applied.selected_percentage, Decimal("21"))

        self.assertEqual(len(invoice.discount_breakdown), 2)
        amounts = sorted(Decimal(e["amount"]) for e in invoice.discount_breakdown)
        self.assertEqual(amounts, [Decimal("94.11"), Decimal("104.56")])

        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["remaining"], Decimal("0.00"))
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("0.00"))
        self.assertEqual(summary["percentage_invoiced"], Decimal("100.00"))


class PartialInvoiceOfDiscountedQuoteTests(_DiscountedQuoteTestBase):

    def _half_items(self):
        return [{
            "source_id": f"config_{self.config.pk}",
            "description": "Corredera 50%",
            "quantity": Decimal("1"),
            "unit_price": Decimal("522.81"),
            "discount_percentage": Decimal("0.00"),
        }]

    def test_half_invoice_scales_discount_and_tax(self):
        invoice = self._create_invoice(self._half_items())
        # ratio = 522.81 / 1045.62 = 0.5 → discount 99.34 (rounded), tax on 423.47
        self.assertEqual(invoice.discount_amount.amount, Decimal("99.34"))
        expected_taxable = Decimal("423.47")  # 522.81 − 99.34
        expected_tax = (expected_taxable * Decimal("0.21")).quantize(Decimal("0.01"))
        self.assertEqual(invoice.tax_amount.amount, expected_tax)
        self.assertEqual(
            invoice.total_amount.amount,
            Decimal("522.81") - Decimal("99.34") + expected_tax,
        )

    def test_two_halves_fully_invoice_the_file(self):
        self._create_invoice(self._half_items())
        second = self._create_invoice(self._half_items())
        self.assertIsNotNone(second.pk)
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        # Per-invoice rounding may leave a cent of drift, never a negative remainder
        self.assertLessEqual(abs(summary["remaining"]), Decimal("0.05"))


class NetSummaryFieldsTests(_DiscountedQuoteTestBase):
    """The *_net summary fields compare discounted bases on both sides, no
    matter whether an invoice carries its discount at line or invoice level."""

    def _make_invoice(self, subtotal, discount):
        from invoices.models import SalesInvoice, SalesInvoiceStatus
        return SalesInvoice.objects.create(
            company=self.company,
            status=SalesInvoiceStatus.PROFORMA,
            client=self.client_obj,
            project_file=self.project_file,
            date_issued=date(2026, 7, 14),
            subtotal_amount=subtotal,
            discount_amount=discount,
            tax_amount=self.TAX,
            total_amount=self.TOTAL,
        )

    def test_invoiceable_net_uses_discounted_subtotal(self):
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["total_invoiceable_subtotal"], self.SUBTOTAL)   # gross
        self.assertEqual(summary["total_invoiceable_subtotal_net"], self.TAXABLE)

    def test_invoice_level_discount_is_netted(self):
        """Wizard-style invoice: gross lines + invoice-level discount."""
        self._make_invoice(subtotal=self.SUBTOTAL, discount=self.DISCOUNT)
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["total_invoiced_subtotal_net"], self.TAXABLE)
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("0.00"))

    def test_line_level_discount_is_netted(self):
        """Invoice whose lines are already discounted (e.g. delivery-order flow)."""
        self._make_invoice(subtotal=self.TAXABLE, discount=Decimal("0.00"))
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["total_invoiced_subtotal_net"], self.TAXABLE)
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("0.00"))
        # The gross comparison would wrongly report the discount as pending
        self.assertEqual(summary["remaining_subtotal"], self.DISCOUNT)


class MultiQuoteDiscountAttributionTests(_DiscountedQuoteTestBase):
    """A discount belongs to its quote: invoicing another quote's items must
    not receive a share of it (regression for invoice #751)."""

    def setUp(self):
        super().setUp()
        from inventory.models import InventoryItem
        from sales.models import QuoteInventoryItem

        # Second accepted quote WITHOUT discount: 400 € + 21% IVA = 484 €
        self.quote2 = make_quote(
            project_file=self.project_file,
            project_name="Plain Quote",
            subtotal_price=Decimal("400.00"),
            tax_amount=Decimal("84.00"),
            total_price=Decimal("484.00"),
        )
        stock_item = InventoryItem.objects.create(name="Prueba 1", company=self.company)
        free_item = InventoryItem.objects.create(name="Intercambiador", company=self.company)
        # 1000 € with a 60% line discount → 400 €
        self.inv_item = QuoteInventoryItem.objects.create(
            quote=self.quote2,
            inventory_item=stock_item,
            quantity=Decimal("1"),
            unit_price=Decimal("1000.00"),
            discount=Decimal("60.00"),
            line_total=Decimal("400.00"),
            total_with_tax=Decimal("484.00"),
        )
        self.free_inv_item = QuoteInventoryItem.objects.create(
            quote=self.quote2,
            inventory_item=free_item,
            quantity=Decimal("1"),
            unit_price=Decimal("0.00"),
            line_total=Decimal("0.00"),
            total_with_tax=Decimal("0.00"),
        )
        from sales.models import QuoteInventoryItem as QII
        qii_ct = ContentType.objects.get_for_model(QII)
        for qii, base, calc in (
            (self.inv_item, Decimal("400.00"), Decimal("84.00")),
            (self.free_inv_item, Decimal("0.00"), Decimal("0.00")),
        ):
            AppliedTax.objects.create(
                content_type=qii_ct,
                object_id=qii.pk,
                tax_type=self.iva21,
                selected_percentage=Decimal("21"),
                base_amount=base,
                calculated_amount=calc,
            )

        # Saving the inventory items triggers a quote recalculation that drops
        # the tax (no quote-level AppliedTax) — pin the intended amounts.
        Quote.objects.filter(pk=self.quote2.pk).update(
            subtotal_price=Decimal("400.00"),
            discounted_subtotal=Decimal("400.00"),
            tax_amount=Decimal("84.00"),
            total_price=Decimal("484.00"),
        )
        self.quote2.refresh_from_db()

    def _quote2_items(self):
        return [
            {
                "source_id": f"inv_item_{self.inv_item.pk}",
                "description": "Prueba 1 (Q#%d)" % self.quote2.pk,
                "quantity": Decimal("1"),
                "unit_price": Decimal("1000.00"),
                "discount_percentage": Decimal("60.00"),
            },
            {
                "source_id": f"inv_item_{self.free_inv_item.pk}",
                "description": "Intercambiador (Q#%d)" % self.quote2.pk,
                "quantity": Decimal("1"),
                "unit_price": Decimal("0.00"),
                "discount_percentage": Decimal("0.00"),
            },
        ]

    def test_no_discount_bleed_into_other_quote(self):
        """Invoice #751 scenario: a zeroed line from the discounted quote plus
        the second quote's items must not inherit quote 1's discount."""
        items = [{
            # zeroed-out line from the discounted quote (user set qty to 0)
            "source_id": f"config_{self.config.pk}",
            "description": "Corredera (zeroed)",
            "quantity": Decimal("0"),
            "unit_price": Decimal("433.02"),
            "discount_percentage": Decimal("0.00"),
        }] + self._quote2_items()

        invoice = self._create_invoice(items)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("400.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("0.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("84.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("484.00"))

    def test_mixed_invoice_scales_each_quote_discount_separately(self):
        """Invoicing both quotes fully applies quote 1's discount only to its
        own lines: tax = 177.86 (on 846.95) + 84.00 (on 400)."""
        invoice = self._create_invoice(self._full_items() + self._quote2_items())
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1445.62"))
        self.assertEqual(invoice.discount_amount.amount, self.DISCOUNT)
        self.assertEqual(invoice.tax_amount.amount, self.TAX + Decimal("84.00"))
        self.assertEqual(
            invoice.total_amount.amount,
            Decimal("1445.62") - self.DISCOUNT + self.TAX + Decimal("84.00"),
        )
        # 1508.81 == the sum of both quote totals → file fully invoiced
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["remaining"], Decimal("0.00"))
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("0.00"))


class UndiscountedQuoteRegressionTests(_DiscountedQuoteTestBase):
    """Quotes without a discount must keep taxing the full subtotal."""

    SUBTOTAL = Decimal("100.00")
    DISCOUNT = Decimal("0.00")
    TAXABLE = Decimal("100.00")
    TAX = Decimal("21.00")
    TOTAL = Decimal("121.00")
    DISCOUNT_BREAKDOWN = []

    def test_tax_on_full_base_without_discount(self):
        invoice = self._create_invoice(self._full_items())
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("0.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("21.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))
        applied = AppliedTax.objects.get(
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
        )
        self.assertEqual(applied.base_amount.amount, Decimal("100.00"))
