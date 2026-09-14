"""
Tests for the invoice discount strategies (invoices.services.discounts).

Each configured discount behaviour lands on the invoice through its own
strategy:
- standard basket rules → invoice-level discount_amount (+ breakdown);
- line-items-basket rules → invoice-level, but scaled only by the invoiced
  line items (never configuration lines);
- financing rules → invoice-level discount_amount too (they are their own
  negative line only on delivery orders);
- line-item rules → carried on the source line's discount_percentage.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, TaxType
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus


class _StrategyTestBase(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF Strategies",
            company=self.company,
            client=self.client_obj,
        )
        self.iva21, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )

    def _make_config(self, quote, subtotal, total, with_tax=True):
        config = Configuration.objects.create(
            name="Config",
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal,
            total_price=total,
        )
        if with_tax:
            AppliedTax.objects.create(
                content_type=ContentType.objects.get_for_model(Configuration),
                object_id=config.pk,
                tax_type=self.iva21,
                selected_percentage=Decimal("21"),
                base_amount=subtotal,
                calculated_amount=(subtotal * Decimal("0.21")).quantize(Decimal("0.01")),
            )
        return config

    def _create_invoice(self, items_data):
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 8, 1),
            due_date=date(2026, 9, 1),
            items_data=items_data,
        )


class FinancingDiscountFoldedIntoHeaderTests(_StrategyTestBase):
    """A financing discount is part of the invoice-level discount — it is
    neither an item-level discount nor a negative invoice line."""

    def setUp(self):
        super().setUp()
        # 1000 subtotal, 100 financing discount, taxable 900, IVA 189, total 1089
        self.quote = Quote.objects.create(
            project_name="Financed Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
            discount_breakdown=[
                {"rule_name": "Financiación 10%", "kind": "financing",
                 "level": "basket", "mode": "net",
                 "percentage": "10.00", "base": "1000.00", "amount": "100.00"},
            ],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )

    def _full_items(self):
        return [{
            "source_id": f"config_{self.config.pk}",
            "description": "Config",
            "quantity": Decimal("1"),
            "unit_price": Decimal("1000.00"),
            "discount_percentage": Decimal("0.00"),
        }]

    def test_financing_does_not_become_a_line(self):
        invoice = self._create_invoice(self._full_items())
        lines = list(invoice.items.all())
        self.assertEqual(len(lines), 1)
        self.assertFalse(
            invoice.items.filter(source_id=f"discount_quote_{self.quote.pk}").exists()
        )

    def test_financing_in_invoice_discount_amount(self):
        invoice = self._create_invoice(self._full_items())
        self.assertEqual(invoice.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(len(invoice.discount_breakdown), 1)
        entry = invoice.discount_breakdown[0]
        self.assertEqual(entry["kind"], "financing")
        self.assertEqual(entry["rule_name"], "Financiación 10%")
        self.assertEqual(Decimal(entry["amount"]), Decimal("100.00"))

    def test_totals_and_tax_on_discounted_base(self):
        invoice = self._create_invoice(self._full_items())
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1089.00"))
        applied = AppliedTax.objects.get(
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
        )
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))

    def test_partial_invoice_scales_financing_discount(self):
        invoice = self._create_invoice([{
            "source_id": f"config_{self.config.pk}",
            "description": "Config 50%",
            "quantity": Decimal("1"),
            "unit_price": Decimal("500.00"),
            "discount_percentage": Decimal("0.00"),
        }])
        self.assertEqual(invoice.discount_amount.amount, Decimal("50.00"))
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("500.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("544.50"))


class MixedStandardAndFinancingTests(_StrategyTestBase):
    """Standard and financing rules both go to discount_amount, each with its
    own breakdown entry — and both reduce the taxable base."""

    def setUp(self):
        super().setUp()
        # 1000 subtotal; standard 100 + financing 50 → taxable 850,
        # IVA 178.50, total 1028.50
        self.quote = Quote.objects.create(
            project_name="Mixed Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("150.00"),
            discounted_subtotal=Decimal("850.00"),
            tax_amount=Decimal("178.50"),
            total_price=Decimal("1028.50"),
            discount_breakdown=[
                {"rule_name": "10%", "kind": "standard", "level": "basket",
                 "mode": "net", "percentage": "10.00",
                 "base": "1000.00", "amount": "100.00"},
                {"rule_name": "Financiación 5%", "kind": "financing",
                 "level": "basket", "mode": "net", "percentage": "5.00",
                 "base": "1000.00", "amount": "50.00"},
            ],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )

    def test_both_land_in_discount_amount(self):
        invoice = self._create_invoice([{
            "source_id": f"config_{self.config.pk}",
            "description": "Config",
            "quantity": Decimal("1"),
            "unit_price": Decimal("1000.00"),
            "discount_percentage": Decimal("0.00"),
        }])
        self.assertEqual(invoice.discount_amount.amount, Decimal("150.00"))
        self.assertEqual(
            [(e["kind"], e["amount"]) for e in invoice.discount_breakdown],
            [("standard", "100.00"), ("financing", "50.00")],
        )
        self.assertEqual(invoice.items.count(), 1)
        # subtotal 1000 − 150 discount + 178.50 tax
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("178.50"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1028.50"))


class LineItemsBasketScopingTests(_StrategyTestBase):
    """A line-items-only discount scales with the invoiced line items and
    never with configuration lines."""

    def setUp(self):
        super().setUp()
        from inventory.models import InventoryItem
        from sales.models import QuoteInventoryItem

        # config 600 + inventory 400; 10% discount on line items only → 40
        self.quote = Quote.objects.create(
            project_name="Line-items discounted",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("1000.00"),
        )
        self.config = self._make_config(
            self.quote, Decimal("600.00"), Decimal("600.00"), with_tax=False
        )
        stock_item = InventoryItem.objects.create(name="Widget", company=self.company)
        self.inv_item = QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=stock_item,
            quantity=Decimal("1"),
            unit_price=Decimal("400.00"),
            line_total=Decimal("400.00"),
        )
        # Item saves trigger a quote recalc — pin the intended amounts.
        Quote.objects.filter(pk=self.quote.pk).update(
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("40.00"),
            discounted_subtotal=Decimal("960.00"),
            tax_amount=Decimal("0.00"),
            total_price=Decimal("960.00"),
            discount_breakdown=[
                {"rule_name": "10% líneas", "kind": "standard",
                 "level": "line_items_basket", "mode": "net",
                 "percentage": "10.00", "base": "400.00", "amount": "40.00"},
            ],
        )
        self.quote.refresh_from_db()

    def test_config_only_invoice_gets_no_line_items_discount(self):
        invoice = self._create_invoice([{
            "source_id": f"config_{self.config.pk}",
            "description": "Config only",
            "quantity": Decimal("1"),
            "unit_price": Decimal("600.00"),
            "discount_percentage": Decimal("0.00"),
        }])
        self.assertEqual(invoice.discount_amount.amount, Decimal("0.00"))

    def test_line_items_invoice_gets_full_line_items_discount(self):
        invoice = self._create_invoice([{
            "source_id": f"inv_item_{self.inv_item.pk}",
            "description": "Widget",
            "quantity": Decimal("1"),
            "unit_price": Decimal("400.00"),
            "discount_percentage": Decimal("0.00"),
        }])
        self.assertEqual(invoice.discount_amount.amount, Decimal("40.00"))


class BulkInvoiceDiscountRegressionTests(_StrategyTestBase):
    """Bulk invoicing used to build every line gross and drop quote discounts
    entirely — the basket discount must now be carried."""

    def setUp(self):
        super().setUp()
        self.quote = Quote.objects.create(
            project_name="Bulk Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
            discount_breakdown=[
                {"rule_name": "10%", "kind": "standard", "level": "basket",
                 "mode": "net", "percentage": "10.00",
                 "base": "1000.00", "amount": "100.00"},
            ],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )

    def test_bulk_invoice_carries_basket_discount(self):
        invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 8, 1),
        )
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(invoice.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(len(invoice.discount_breakdown), 1)
        self.assertEqual(invoice.tax_amount.amount, Decimal("189.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1089.00"))
        applied = AppliedTax.objects.get(
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
        )
        self.assertEqual(applied.base_amount.amount, Decimal("900.00"))


class LegacyBreakdownFallbackTests(_StrategyTestBase):
    """A quote with a discount_amount but no usable breakdown (older data,
    manual quotes) is treated as a single basket-level discount."""

    def setUp(self):
        super().setUp()
        self.quote = Quote.objects.create(
            project_name="Legacy Quote",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("1000.00"),
            discount_amount=Decimal("100.00"),
            discounted_subtotal=Decimal("900.00"),
            tax_amount=Decimal("189.00"),
            total_price=Decimal("1089.00"),
            discount_breakdown=[],
        )
        self.config = self._make_config(
            self.quote, Decimal("1000.00"), Decimal("1210.00")
        )

    def test_discount_carried_as_basket(self):
        invoice = self._create_invoice([{
            "source_id": f"config_{self.config.pk}",
            "description": "Config",
            "quantity": Decimal("1"),
            "unit_price": Decimal("1000.00"),
            "discount_percentage": Decimal("0.00"),
        }])
        self.assertEqual(invoice.discount_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("1089.00"))
