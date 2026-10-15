"""
Tests for auto-apply client taxes feature.

- Configuration.copy_taxes_from_client() replaces product taxes when flag ON
- copy_taxes_from_client() is a no-op when flag is OFF
- copy_taxes_from_client() is a no-op when client has no taxes
- QuoteInventoryItem uses client taxes when flag is ON
- QuoteInventoryItem falls back to item taxes when flag is OFF
"""
from decimal import Decimal
from django.test import TestCase
from django.contrib.contenttypes.models import ContentType

from configurator.models import ProductTemplate
from invoices.models import AppliedTax, TaxType
from sales.tests.factories import (
    apply_taxes,
    make_client,
    make_company,
    make_configuration,
    make_inventory_item,
    make_quote,
    make_quote_inventory_line,
)


class _BaseTestCase(TestCase):
    """Shared fixtures for auto-tax tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Tax Co")

        cls.vat_21, _ = TaxType.objects.get_or_create(
            tax_name="IVA 21",
            country="ES",
            defaults={
                "behavior": "add",
                "percentage_options": [0, 4, 10, 21],
                "default_percentage": 21,
            },
        )
        cls.vat_10, _ = TaxType.objects.get_or_create(
            tax_name="IVA Reducido",
            country="ES",
            defaults={
                "behavior": "add",
                "percentage_options": [0, 10],
                "default_percentage": 10,
            },
        )
        cls.irpf, _ = TaxType.objects.get_or_create(
            tax_name="IRPF",
            country="ES",
            defaults={
                "behavior": "subtract",
                "percentage_options": [0, 15],
                "default_percentage": 15,
            },
        )

        cls.product = ProductTemplate.objects.create(
            name="Aluminium Window",
            company=cls.company,
        )

        # Attach IVA 21% as default tax on the product
        apply_taxes(cls.product, taxes=[(cls.vat_21, Decimal("21.00"))])


# ---------------------------------------------------------------------------
# Configuration.copy_taxes_from_client()
# ---------------------------------------------------------------------------

class ConfigurationCopyTaxesFromClientTest(_BaseTestCase):
    """Test Configuration.copy_taxes_from_client() logic."""

    def _attach_client_taxes(self, client):
        """Add IVA 10% + IRPF 15% as default taxes on a client."""
        apply_taxes(client, taxes=[(self.vat_10, Decimal("10.00")), (self.irpf, Decimal("15.00"))])

    def test_flag_off_does_nothing(self):
        """When auto_apply_client_taxes is OFF, copy_taxes_from_client is a no-op."""
        self.company.auto_apply_client_taxes = False
        self.company.save()

        client = make_client(company=self.company, name="Client A")
        self._attach_client_taxes(client)

        config = make_configuration(
            company=self.company, client=client, product=self.product, name="Config A",
        )
        config.copy_default_taxes_from_product()
        config.copy_taxes_from_client()

        # Should still have the product's tax (IVA 21%), not the client's
        taxes = list(config.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)
        self.assertEqual(taxes[0].selected_percentage, Decimal("21.00"))

    def test_flag_on_replaces_product_taxes(self):
        """When flag is ON and client has taxes, they replace product taxes."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        client = make_client(company=self.company, name="Client B")
        self._attach_client_taxes(client)

        config = make_configuration(
            company=self.company, client=client, product=self.product, name="Config B",
        )
        config.copy_default_taxes_from_product()
        config.copy_taxes_from_client()

        taxes = list(config.get_taxes())
        tax_types = {t.tax_type for t in taxes}
        self.assertEqual(len(taxes), 2)
        self.assertIn(self.vat_10, tax_types)
        self.assertIn(self.irpf, tax_types)
        # Product's IVA 21% should have been replaced
        self.assertNotIn(self.vat_21, tax_types)

    def test_flag_on_but_no_client_taxes_keeps_product_taxes(self):
        """When flag is ON but client has no taxes, product taxes remain."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        client = make_client(company=self.company, name="Client C")

        config = make_configuration(
            company=self.company, client=client, product=self.product, name="Config C",
        )
        config.copy_default_taxes_from_product()
        config.copy_taxes_from_client()

        taxes = list(config.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)

    def test_no_client_on_config(self):
        """When configuration has no client, copy_taxes_from_client is a no-op."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        config = make_configuration(
            company=self.company, client=None, product=self.product, name="Config No Client",
        )
        config.copy_default_taxes_from_product()
        config.copy_taxes_from_client()

        taxes = list(config.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)

    def test_client_taxes_order_preserved(self):
        """Client tax order values are preserved when copied."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        client = make_client(company=self.company, name="Client D")
        ct = ContentType.objects.get_for_model(client)
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.irpf, selected_percentage=Decimal("15.00"), order=5,
        )
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.vat_10, selected_percentage=Decimal("10.00"), order=10,
        )

        config = make_configuration(
            company=self.company, client=client, product=self.product, name="Config D",
        )
        config.copy_default_taxes_from_product()
        config.copy_taxes_from_client()

        taxes = list(config.get_taxes())
        self.assertEqual(taxes[0].order, 5)
        self.assertEqual(taxes[1].order, 10)


# ---------------------------------------------------------------------------
# QuoteInventoryItem client tax override
# ---------------------------------------------------------------------------

class QuoteInventoryItemClientTaxTest(_BaseTestCase):
    """Test QuoteInventoryItem uses client taxes when company flag is ON."""

    def setUp(self):
        self.client = make_client(company=self.company, name="Quote Client")
        self.quote = make_quote(company=self.company, client=self.client, project_name="Tax Test Quote")
        # The inventory item carries IVA 21%
        self.inv_item = make_inventory_item(company=self.company, taxes=[(self.vat_21, Decimal("21.00"))])

    def _attach_client_taxes(self):
        apply_taxes(self.client, taxes=[(self.vat_10, Decimal("10.00"))])

    def test_flag_off_uses_item_taxes(self):
        """When flag is OFF, QuoteInventoryItem gets the item's own taxes."""
        self.company.auto_apply_client_taxes = False
        self.company.save()

        qii = make_quote_inventory_line(
            quote=self.quote, item=self.inv_item, quantity=Decimal("2"), unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)

    def test_flag_on_uses_client_taxes(self):
        """When flag is ON and client has taxes, those replace the item taxes."""
        self.company.auto_apply_client_taxes = True
        self.company.save()
        self._attach_client_taxes()

        qii = make_quote_inventory_line(
            quote=self.quote, item=self.inv_item, quantity=Decimal("2"), unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_10)
        self.assertEqual(taxes[0].selected_percentage, Decimal("10.00"))

    def test_flag_on_no_client_taxes_falls_back_to_item(self):
        """When flag is ON but client has no taxes, item taxes are used."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        qii = make_quote_inventory_line(
            quote=self.quote, item=self.inv_item, quantity=Decimal("2"), unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)

    def test_flag_on_no_client_on_quote_falls_back_to_item(self):
        """When flag is ON but quote has no client, item taxes are used."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        quote_no_client = make_quote(company=self.company, project_name="No Client Quote")
        qii = make_quote_inventory_line(
            quote=quote_no_client, item=self.inv_item, quantity=Decimal("1"), unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)
