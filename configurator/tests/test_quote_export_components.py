"""
Tests that a Quote export exposes each line's Bill of Materials (the
"Componente"/"Cantidad" list on the InventoryItem admin page) as a
"components" key per line, so a quote template can optionally list what's
inside a kit-type item (e.g. a solar water heater sold as one line, built
from a tank, panels, a support structure, etc.).
"""
from decimal import Decimal

from django.test import TestCase

from company.models import Company
from configurator.services.export.quote import QuoteExportStrategy
from inventory.models import BillOfMaterialsLine, InventoryItem
from sales.models import Client, Quote, QuoteInventoryItem


class QuoteExportComponentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Components Export Co")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)

    def _make_quote(self):
        return Quote.objects.create(
            client=self.client_obj,
            company=self.company,
            project_name="Components Export Test",
        )

    def _get_line(self, quote):
        context = QuoteExportStrategy(quote).get_context(include_rendered_templates=False)
        return context["inventory_items"][0]

    def test_kit_item_exposes_its_components(self):
        kit = InventoryItem.objects.create(
            name="Drain Back ACS 500 L", code="0460184", company=self.company,
            selling_price=Decimal("100.00"),
        )
        tank = InventoryItem.objects.create(
            name="Interacumulador ACS Mod. 500", code="0550012", company=self.company,
            selling_price=Decimal("10.00"),
        )
        panel = InventoryItem.objects.create(
            name="Captador Solar Plano PROMASUN", code="0420002", company=self.company,
            selling_price=Decimal("10.00"),
        )
        BillOfMaterialsLine.objects.create(parent=kit, component=tank, quantity=1)
        BillOfMaterialsLine.objects.create(parent=kit, component=panel, quantity=3)

        quote = self._make_quote()
        QuoteInventoryItem.objects.create(
            quote=quote, inventory_item=kit, quantity=Decimal("1"), unit_price=Decimal("100.00"),
        )

        line = self._get_line(quote)
        self.assertTrue(line["has_components"])
        self.assertEqual(
            {(c["code"], c["quantity"]) for c in line["components"]},
            {("0550012", 1), ("0420002", 3)},
        )
        tank_component = next(c for c in line["components"] if c["code"] == "0550012")
        self.assertEqual(tank_component["name"], str(tank))

    def test_plain_item_has_no_components(self):
        item = InventoryItem.objects.create(
            name="Simple Item", code="0010000", company=self.company,
            selling_price=Decimal("5.00"),
        )
        quote = self._make_quote()
        QuoteInventoryItem.objects.create(
            quote=quote, inventory_item=item, quantity=Decimal("1"), unit_price=Decimal("5.00"),
        )

        line = self._get_line(quote)
        self.assertFalse(line["has_components"])
        self.assertEqual(line["components"], [])
