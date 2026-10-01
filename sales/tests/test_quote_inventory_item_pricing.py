"""QuoteInventoryItem unit_price follows the inventory item on create and on
item swap, but is left alone when the line is edited with the same item."""
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from inventory.models import InventoryItem
from sales.models import Client, Quote, QuoteInventoryItem


class QuoteInventoryItemPricingTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Pricing Co")
        cls.client_obj = Client.objects.create(name="Client", company=cls.company)
        cls.item_a = InventoryItem.objects.create(
            name="A", company=cls.company, selling_price=Money("10.00", "EUR")
        )
        cls.item_b = InventoryItem.objects.create(
            name="B", company=cls.company, selling_price=Money("25.00", "EUR")
        )
        cls.item_unpriced = InventoryItem.objects.create(name="C", company=cls.company)

    def setUp(self):
        self.quote = Quote.objects.create(
            client=self.client_obj, company=self.company, project_name="Q"
        )
        self.line = QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=self.item_a, quantity=Decimal("2")
        )

    def test_create_fills_price_from_item(self):
        self.assertEqual(self.line.unit_price.amount, Decimal("10.00"))

    def test_same_item_keeps_existing_price(self):
        # Item price changes after the quote line was priced
        self.item_a.selling_price = Money("99.00", "EUR")
        self.item_a.save()

        line = QuoteInventoryItem.objects.get(pk=self.line.pk)
        line.quantity = Decimal("3")
        line.save()

        line.refresh_from_db()
        self.assertEqual(line.unit_price.amount, Decimal("10.00"))
        self.assertEqual(line.line_total.amount, Decimal("30.00"))

    def test_swapping_item_grabs_new_price(self):
        line = QuoteInventoryItem.objects.get(pk=self.line.pk)
        line.inventory_item = self.item_b
        line.save()

        line.refresh_from_db()
        self.assertEqual(line.unit_price.amount, Decimal("25.00"))
        self.assertEqual(line.line_total.amount, Decimal("50.00"))

    def test_swapping_to_unpriced_item_resets_price(self):
        line = QuoteInventoryItem.objects.get(pk=self.line.pk)
        line.inventory_item = self.item_unpriced
        line.save()

        line.refresh_from_db()
        self.assertEqual(line.unit_price.amount, Decimal("0.00"))
