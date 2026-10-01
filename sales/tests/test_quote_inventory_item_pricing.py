"""QuoteInventoryItem unit_price and taxes follow the inventory item on create
and on item swap, but are left alone when the line is edited with the same item."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
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

        cls.vat_21, _ = TaxType.objects.get_or_create(
            tax_name="IVA 21", country="ES",
            defaults={"behavior": "add", "percentage_options": [0, 21], "default_percentage": 21},
        )
        cls.vat_10, _ = TaxType.objects.get_or_create(
            tax_name="IVA Reducido", country="ES",
            defaults={"behavior": "add", "percentage_options": [0, 10], "default_percentage": 10},
        )
        cls._attach_tax(cls.item_a, cls.vat_21, "21.00")
        cls._attach_tax(cls.item_b, cls.vat_10, "10.00")

    @staticmethod
    def _attach_tax(obj, tax_type, pct):
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk,
            tax_type=tax_type, selected_percentage=Decimal(pct), order=0,
        )

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

    def test_swapping_item_replaces_taxes(self):
        line = QuoteInventoryItem.objects.get(pk=self.line.pk)
        line.inventory_item = self.item_b
        line.save()

        taxes = list(line.get_taxes())
        self.assertEqual([t.tax_type for t in taxes], [self.vat_10])
        line.refresh_from_db()
        # 2 x 25.00 = 50.00 + 10% IVA
        self.assertEqual(line.total_with_tax.amount, Decimal("55.00"))

    def test_same_item_keeps_edited_taxes(self):
        # User hand-edits the line's tax to the reduced rate
        tax = self.line.get_taxes().get()
        tax.tax_type = self.vat_10
        tax.selected_percentage = Decimal("10.00")
        tax.save()

        line = QuoteInventoryItem.objects.get(pk=self.line.pk)
        line.quantity = Decimal("3")
        line.save()

        self.assertEqual([t.tax_type for t in line.get_taxes()], [self.vat_10])
