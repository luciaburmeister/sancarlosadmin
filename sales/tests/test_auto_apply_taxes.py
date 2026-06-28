"""
Tests for auto-apply client taxes feature.

- Company.auto_apply_client_taxes flag defaults to False
- Client can have AppliedTax records via GenericFK
- Configuration.copy_taxes_from_client() replaces product taxes when flag ON
- copy_taxes_from_client() is a no-op when flag is OFF
- copy_taxes_from_client() is a no-op when client has no taxes
- QuoteInventoryItem uses client taxes when flag is ON
- QuoteInventoryItem falls back to item taxes when flag is OFF
"""
from decimal import Decimal
from django.test import TestCase
from django.contrib.contenttypes.models import ContentType

from company.models import Company
from sales.models import Client, Quote, QuoteInventoryItem
from configurator.models import ProductTemplate, Configuration
from invoices.models import TaxType, AppliedTax
from inventory.models import InventoryItem


class _BaseTestCase(TestCase):
    """Shared fixtures for auto-tax tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tax Co")

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
        product_ct = ContentType.objects.get_for_model(cls.product)
        AppliedTax.objects.create(
            content_type=product_ct,
            object_id=cls.product.pk,
            tax_type=cls.vat_21,
            selected_percentage=Decimal("21.00"),
            order=0,
        )


# ---------------------------------------------------------------------------
# Company Flag
# ---------------------------------------------------------------------------

class CompanyAutoTaxFlagTest(_BaseTestCase):
    """Test Company.auto_apply_client_taxes flag."""

    def test_flag_defaults_to_false(self):
        company = Company.objects.create(name="New Co")
        self.assertFalse(company.auto_apply_client_taxes)

    def test_flag_can_be_enabled(self):
        self.company.auto_apply_client_taxes = True
        self.company.save()
        self.company.refresh_from_db()
        self.assertTrue(self.company.auto_apply_client_taxes)


# ---------------------------------------------------------------------------
# Client AppliedTax via GenericFK
# ---------------------------------------------------------------------------

class ClientAppliedTaxTest(_BaseTestCase):
    """Test that AppliedTax can be attached to a Client via GenericFK."""

    def test_add_tax_to_client(self):
        client = Client.objects.create(name="Taxable Client", company=self.company)
        ct = ContentType.objects.get_for_model(client)
        tax = AppliedTax.objects.create(
            content_type=ct,
            object_id=client.pk,
            tax_type=self.vat_21,
            selected_percentage=Decimal("21.00"),
            order=0,
        )
        self.assertEqual(tax.content_object, client)

    def test_client_can_have_multiple_taxes(self):
        client = Client.objects.create(name="Multi-Tax Client", company=self.company)
        ct = ContentType.objects.get_for_model(client)
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.vat_21, selected_percentage=Decimal("21.00"), order=0,
        )
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.irpf, selected_percentage=Decimal("15.00"), order=1,
        )
        taxes = AppliedTax.objects.filter(content_type=ct, object_id=client.pk)
        self.assertEqual(taxes.count(), 2)


# ---------------------------------------------------------------------------
# Configuration.copy_taxes_from_client()
# ---------------------------------------------------------------------------

class ConfigurationCopyTaxesFromClientTest(_BaseTestCase):
    """Test Configuration.copy_taxes_from_client() logic."""

    def _attach_client_taxes(self, client):
        """Add IVA 10% + IRPF 15% as default taxes on a client."""
        ct = ContentType.objects.get_for_model(client)
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.vat_10, selected_percentage=Decimal("10.00"), order=0,
        )
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.irpf, selected_percentage=Decimal("15.00"), order=1,
        )

    def test_flag_off_does_nothing(self):
        """When auto_apply_client_taxes is OFF, copy_taxes_from_client is a no-op."""
        self.company.auto_apply_client_taxes = False
        self.company.save()

        client = Client.objects.create(name="Client A", company=self.company)
        self._attach_client_taxes(client)

        config = Configuration.objects.create(
            company=self.company, client=client,
            product=self.product, name="Config A",
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

        client = Client.objects.create(name="Client B", company=self.company)
        self._attach_client_taxes(client)

        config = Configuration.objects.create(
            company=self.company, client=client,
            product=self.product, name="Config B",
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

        client = Client.objects.create(name="Client C", company=self.company)

        config = Configuration.objects.create(
            company=self.company, client=client,
            product=self.product, name="Config C",
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

        config = Configuration.objects.create(
            company=self.company, client=None,
            product=self.product, name="Config No Client",
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

        client = Client.objects.create(name="Client D", company=self.company)
        ct = ContentType.objects.get_for_model(client)
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.irpf, selected_percentage=Decimal("15.00"), order=5,
        )
        AppliedTax.objects.create(
            content_type=ct, object_id=client.pk,
            tax_type=self.vat_10, selected_percentage=Decimal("10.00"), order=10,
        )

        config = Configuration.objects.create(
            company=self.company, client=client,
            product=self.product, name="Config D",
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
        self.client = Client.objects.create(
            name="Quote Client",
            company=self.company,
        )
        self.quote = Quote.objects.create(
            client=self.client,
            company=self.company,
            project_name="Tax Test Quote",
        )
        self.inv_item = InventoryItem.objects.create(
            name="Widget",
            company=self.company,
        )
        # Attach IVA 21% to the inventory item
        inv_ct = ContentType.objects.get_for_model(self.inv_item)
        AppliedTax.objects.create(
            content_type=inv_ct, object_id=self.inv_item.pk,
            tax_type=self.vat_21, selected_percentage=Decimal("21.00"), order=0,
        )

    def _attach_client_taxes(self):
        ct = ContentType.objects.get_for_model(self.client)
        AppliedTax.objects.create(
            content_type=ct, object_id=self.client.pk,
            tax_type=self.vat_10, selected_percentage=Decimal("10.00"), order=0,
        )

    def test_flag_off_uses_item_taxes(self):
        """When flag is OFF, QuoteInventoryItem gets the item's own taxes."""
        self.company.auto_apply_client_taxes = False
        self.company.save()

        qii = QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=self.inv_item,
            quantity=Decimal("2"),
            unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)

    def test_flag_on_uses_client_taxes(self):
        """When flag is ON and client has taxes, those replace the item taxes."""
        self.company.auto_apply_client_taxes = True
        self.company.save()
        self._attach_client_taxes()

        qii = QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=self.inv_item,
            quantity=Decimal("2"),
            unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_10)
        self.assertEqual(taxes[0].selected_percentage, Decimal("10.00"))

    def test_flag_on_no_client_taxes_falls_back_to_item(self):
        """When flag is ON but client has no taxes, item taxes are used."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        qii = QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=self.inv_item,
            quantity=Decimal("2"),
            unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)

    def test_flag_on_no_client_on_quote_falls_back_to_item(self):
        """When flag is ON but quote has no client, item taxes are used."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        quote_no_client = Quote.objects.create(
            company=self.company,
            project_name="No Client Quote",
        )
        qii = QuoteInventoryItem.objects.create(
            quote=quote_no_client,
            inventory_item=self.inv_item,
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
        )
        taxes = list(qii.get_taxes())
        self.assertEqual(len(taxes), 1)
        self.assertEqual(taxes[0].tax_type, self.vat_21)
