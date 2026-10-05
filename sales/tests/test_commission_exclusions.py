"""Products and inventory items marked "No commission" (portes) stay out of the commission base."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from configurator.commission.models import InvoiceCommission
from configurator.models import ProductTemplate
from inventory.models import InventoryItem
from invoices.models import AppliedTax
from sales.models import DiscountKind, DiscountLevel, DiscountMode, DiscountPolicy, DiscountRule, QuoteInventoryItem
from sales.tests.test_commission_releases import _CommissionReleaseTestCase


class NoCommissionTest(_CommissionReleaseTestCase):
    """The 1,000.00 quote at 10 % plus 200.00 of portes."""

    def add_portes_config(self, subtotal=Decimal("200.00")):
        portes = ProductTemplate.objects.create(
            name="Portes Automáticos", company=self.company, exclude_from_commission=True,
        )
        product, self.product = self.product, portes
        try:
            return self.add_config(self.quote, subtotal)
        finally:
            self.product = product

    def add_portes_item(self, price=Decimal("200.00")):
        item = InventoryItem.objects.create(name="Portes", company=self.company, exclude_from_commission=True)
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(InventoryItem),
            object_id=item.pk, tax_type=self.iva, selected_percentage=Decimal("21"),
        )
        QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=item, quantity=Decimal("1"), unit_price=price,
        )
        self.quote.refresh_from_db()

    def commission_amount(self):
        self.commission.refresh_from_db()
        return self.commission.commission_amount.amount

    def test_portes_configuration_earns_no_commission(self):
        self.add_portes_config()
        self.assertEqual(self.commission_amount(), Decimal("100.00"))

        invoice = self.final_goods_invoice()
        self.pay(invoice, invoice.total_amount.amount)
        row = InvoiceCommission.objects.get(sales_invoice=invoice)
        self.assertEqual(row.base_amount.amount, Decimal("1000.00"))
        self.assertEqual(row.released_amount.amount, Decimal("100.00"))
        self.assertEqual(row.commission_percentage, Decimal("10.00"))

    def test_portes_inventory_item_earns_no_commission(self):
        self.add_portes_item()
        self.assertEqual(self.commission_amount(), Decimal("100.00"))

        invoice = self.final_goods_invoice()
        self.pay(invoice, invoice.total_amount.amount)
        row = InvoiceCommission.objects.get(sales_invoice=invoice)
        self.assertEqual(row.base_amount.amount, Decimal("1000.00"))
        self.assertEqual(row.released_amount.amount, Decimal("100.00"))

    def test_advance_on_a_quote_with_portes_releases_only_its_commission(self):
        self.add_portes_config()
        advance, advance_line = self.make_advance(Decimal("360.00"), "30")  # 30 % of 1,200
        self.pay(advance, advance.total_amount.amount)
        self.assertEqual(self.released(), Decimal("30.00"))

        invoice = self.final_goods_invoice(deductions=[(advance_line, Decimal("360.00"))])
        self.pay(invoice, invoice.total_amount.amount)
        self.assertEqual(self.released(), Decimal("100.00"))

    def test_financing_on_line_items_is_not_taken_off_the_portes(self):
        """Like INV-2026-00028: 2 % financing on line items only, plus portes."""
        policy = DiscountPolicy.objects.create(name="Financiación", company=self.company)
        DiscountRule.objects.create(
            policy=policy, name="Financiación 2%", order=0, percentage=Decimal("2.00"),
            mode=DiscountMode.ACCUMULATED, level=DiscountLevel.LINE_ITEMS_BASKET,
            kind=DiscountKind.FINANCING,
        )
        self.quote.discount_policies.add(policy)
        item = InventoryItem.objects.create(name="Acumulador", company=self.company)
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(InventoryItem),
            object_id=item.pk, tax_type=self.iva, selected_percentage=Decimal("21"),
        )
        QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=item, quantity=Decimal("1"), unit_price=Decimal("500.00"),
        )
        self.add_portes_config()  # re-totals the quote: 10.00 of financing, all on the 500.00 item

        # 1,000.00 configuration + 490.00 item, at 10 %
        self.assertEqual(self.commission_amount(), Decimal("149.00"))
        invoice = self.final_goods_invoice()
        self.pay(invoice, invoice.total_amount.amount)
        row = InvoiceCommission.objects.get(sales_invoice=invoice)
        self.assertEqual(row.base_amount.amount, Decimal("1490.00"))
        self.assertEqual(row.released_amount.amount, Decimal("149.00"))
        self.assertEqual(row.commission_percentage, Decimal("10.00"))
