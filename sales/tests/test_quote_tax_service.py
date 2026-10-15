"""
Tests for QuoteTaxService strategy pattern.

Covers both strategies:
- ClientTaxStrategy: taxes from client, base = discounted_subtotal
- SourceTaxStrategy: taxes from each config/item, per-source bases

Key scenarios:
- Single tax type, no discount
- Single tax type, with discount
- Multiple tax types (e.g. IVA + IRPF)
- Mixed configs: some with taxes, some without
- Inventory items with taxes
- Stale taxes get removed when configs change
- Correct strategy is selected based on company flag
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from configurator.models import Configuration, ProductTemplate
from invoices.models import AppliedTax, TaxType
from sales.models import Quote, QuoteInventoryItem
from sales.services.quote_tax_service import (
    ClientTaxStrategy,
    QuoteTaxService,
    SourceTaxStrategy,
)
from sales.tests.factories import (
    apply_taxes,
    make_client,
    make_company,
    make_configuration,
    make_inventory_item,
    make_quote,
)


class _QuoteTaxBase(TestCase):
    """Shared fixtures for quote tax strategy tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Tax Strategy Co")

        cls.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA",
            country="ES",
            defaults={
                "behavior": "add",
                "percentage_options": [0, 10, 21],
                "default_percentage": 21,
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
            name="Test Product",
            company=cls.company,
        )

    def _make_client(self, taxes=()):
        return make_client(company=self.company, taxes=taxes)

    def _make_quote(self, client=None):
        return make_quote(company=self.company, client=client, project_name="Tax Test Quote")

    def _make_config(self, quote, subtotal, taxes=()):
        return make_configuration(quote=quote, subtotal=subtotal, product=self.product, taxes=taxes)

    def _make_inv_item(self, quote, unit_price, quantity, taxes=()):
        """A quote line with ``taxes``, saved without the item-tax auto-copy."""
        qii = QuoteInventoryItem(
            quote=quote, inventory_item=make_inventory_item(company=self.company),
            quantity=quantity, unit_price=unit_price,
        )
        qii.line_total = unit_price * quantity * ((Decimal("100") - qii.discount) / Decimal("100"))
        qii.total_with_tax = qii.line_total
        super(QuoteInventoryItem, qii).save()
        apply_taxes(qii, taxes=taxes)
        return qii

    def _quote_taxes(self, quote):
        """Return quote-level AppliedTax records."""
        ct = ContentType.objects.get_for_model(quote)
        return AppliedTax.objects.filter(
            content_type=ct, object_id=quote.pk,
        ).select_related("tax_type").order_by("order")


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------

class QuoteTaxServiceStrategySelectionTest(_QuoteTaxBase):
    """QuoteTaxService picks the right strategy based on company flag."""

    def test_flag_on_returns_client_strategy(self):
        self.company.auto_apply_client_taxes = True
        self.company.save()
        client = self._make_client()
        quote = self._make_quote(client)
        strategy = QuoteTaxService.get_strategy(quote)
        self.assertIsInstance(strategy, ClientTaxStrategy)

    def test_flag_off_returns_source_strategy(self):
        self.company.auto_apply_client_taxes = False
        self.company.save()
        client = self._make_client()
        quote = self._make_quote(client)
        strategy = QuoteTaxService.get_strategy(quote)
        self.assertIsInstance(strategy, SourceTaxStrategy)

    def test_no_company_returns_source_strategy(self):
        quote = Quote.objects.create(project_name="No Company Quote")
        strategy = QuoteTaxService.get_strategy(quote)
        self.assertIsInstance(strategy, SourceTaxStrategy)


# ---------------------------------------------------------------------------
# ClientTaxStrategy tests
# ---------------------------------------------------------------------------

class ClientTaxStrategyTest(_QuoteTaxBase):
    """Tests for ClientTaxStrategy (auto_apply_client_taxes = ON)."""

    def setUp(self):
        self.company.auto_apply_client_taxes = True
        self.company.save()

    def test_single_tax_no_discount(self):
        """Base = discounted_subtotal when there's no discount, however the
        1000.00 is split across configs and inventory items."""
        iva21 = [(self.iva, Decimal("21.00"))]

        def single_config(quote):
            self._make_config(quote, Decimal("1000.00"), taxes=iva21)

        def two_configs(quote):
            self._make_config(quote, Decimal("600.00"), taxes=iva21)
            self._make_config(quote, Decimal("400.00"), taxes=iva21)

        def config_plus_inventory_item(quote):
            self._make_config(quote, Decimal("500.00"), taxes=iva21)
            self._make_inv_item(quote, Decimal("100.00"), Decimal("5"), taxes=iva21)

        for build_sources in (single_config, two_configs, config_plus_inventory_item):
            with self.subTest(sources=build_sources.__name__):
                client = self._make_client(taxes=iva21)
                quote = self._make_quote(client)
                build_sources(quote)

                raw = Decimal("1000.00")
                discounted = Decimal("1000.00")
                tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

                self.assertEqual(tax_amount, Decimal("210.00"))
                self.assertEqual(len(breakdown), 1)
                self.assertEqual(breakdown[0]["base"], "1000.00")
                self.assertEqual(breakdown[0]["tax_amount"], "210.00")

    def test_single_tax_with_discount(self):
        """Base = discounted_subtotal (subtotal - discount)."""
        client = self._make_client(taxes=[(self.iva, Decimal("21.00"))])
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("1000.00"),
                          taxes=[(self.iva, Decimal("21.00"))])

        raw = Decimal("1000.00")
        discounted = Decimal("800.00")  # 20% discount
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        self.assertEqual(tax_amount, Decimal("168.00"))
        self.assertEqual(breakdown[0]["base"], "800.00")

    def test_multiple_taxes_same_base(self):
        """Multiple taxes all get discounted_subtotal as their base."""
        client = self._make_client(taxes=[
            (self.iva, Decimal("21.00")),
            (self.irpf, Decimal("15.00")),
        ])
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("1000.00"), taxes=[
            (self.iva, Decimal("21.00")),
            (self.irpf, Decimal("15.00")),
        ])

        raw = Decimal("1000.00")
        discounted = Decimal("900.00")
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        # IVA: 900 * 21% = 189.00, IRPF: 900 * 15% = 135.00
        iva_entry = next(b for b in breakdown if b["tax_name"] == "IVA")
        irpf_entry = next(b for b in breakdown if b["tax_name"] == "IRPF")
        self.assertEqual(iva_entry["base"], "900.00")
        self.assertEqual(iva_entry["tax_amount"], "189.00")
        self.assertEqual(irpf_entry["base"], "900.00")
        self.assertEqual(irpf_entry["tax_amount"], "135.00")
        # The IRPF retention is subtracted; its breakdown entry stays positive.
        self.assertEqual(tax_amount, Decimal("189.00") - Decimal("135.00"))

    def test_stale_taxes_removed(self):
        """When a config's tax type changes, stale quote taxes are removed."""
        client = self._make_client(taxes=[(self.iva, Decimal("21.00"))])
        quote = self._make_quote(client)
        config = self._make_config(quote, Decimal("1000.00"),
                                   taxes=[(self.iva, Decimal("21.00"))])

        # First apply — creates IVA
        QuoteTaxService.apply(quote, Decimal("1000"), Decimal("1000"))
        self.assertEqual(self._quote_taxes(quote).count(), 1)

        # Remove IVA from config, add IRPF
        config_ct = ContentType.objects.get_for_model(config)
        AppliedTax.objects.filter(content_type=config_ct, object_id=config.pk).delete()
        AppliedTax.objects.create(
            content_type=config_ct, object_id=config.pk,
            tax_type=self.irpf, selected_percentage=Decimal("15.00"), order=0,
        )

        # Re-apply — IVA should be gone, IRPF should be there
        QuoteTaxService.apply(quote, Decimal("1000"), Decimal("1000"))
        taxes = self._quote_taxes(quote)
        self.assertEqual(taxes.count(), 1)
        self.assertEqual(taxes.first().tax_type, self.irpf)

    def test_base_is_not_clamped_at_zero(self):
        """The strategy takes discounted_subtotal as the base verbatim: a
        negative one (discount larger than the subtotal) is passed through,
        not clamped to zero. Clamping, if wanted, belongs to the caller."""
        client = self._make_client(taxes=[(self.iva, Decimal("21.00"))])
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("100.00"),
                          taxes=[(self.iva, Decimal("21.00"))])

        raw = Decimal("100.00")
        discounted = Decimal("-50.00")  # extreme edge case
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        self.assertEqual(breakdown[0]["base"], "-50.00")

    def test_zero_subtotal(self):
        """Zero subtotal should produce zero tax."""
        client = self._make_client(taxes=[(self.iva, Decimal("21.00"))])
        quote = self._make_quote(client)

        tax_amount, breakdown = QuoteTaxService.apply(
            quote, Decimal("0"), Decimal("0"),
        )
        self.assertEqual(tax_amount, Decimal("0.00"))
        self.assertEqual(len(breakdown), 0)


# ---------------------------------------------------------------------------
# SourceTaxStrategy tests
# ---------------------------------------------------------------------------

class SourceTaxStrategyTest(_QuoteTaxBase):
    """Tests for SourceTaxStrategy (auto_apply_client_taxes = OFF)."""

    def setUp(self):
        self.company.auto_apply_client_taxes = False
        self.company.save()

    def test_single_config_single_tax_no_discount(self):
        """Per-source base = config subtotal, no discount."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("1000.00"),
                          taxes=[(self.iva, Decimal("21.00"))])

        raw = Decimal("1000.00")
        discounted = Decimal("1000.00")
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        self.assertEqual(tax_amount, Decimal("210.00"))
        self.assertEqual(breakdown[0]["base"], "1000.00")

    def test_single_config_with_discount(self):
        """Per-source base is scaled by discount factor."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("1000.00"),
                          taxes=[(self.iva, Decimal("21.00"))])

        raw = Decimal("1000.00")
        discounted = Decimal("800.00")  # 20% discount
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        # base = 1000 * (800/1000) = 800
        self.assertEqual(breakdown[0]["base"], "800.00")
        self.assertEqual(tax_amount, Decimal("168.00"))

    def test_two_configs_different_taxes(self):
        """Different configs can have different tax types."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("600.00"),
                          taxes=[(self.iva, Decimal("21.00"))])
        self._make_config(quote, Decimal("400.00"),
                          taxes=[(self.iva, Decimal("10.00"))])

        raw = Decimal("1000.00")
        discounted = Decimal("1000.00")
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        # IVA 21% on base 600 = 126, IVA 10% on base 400 = 40
        self.assertEqual(len(breakdown), 2)
        b21 = next(b for b in breakdown if b["percentage"] == "21.00")
        b10 = next(b for b in breakdown if b["percentage"] == "10.00")
        self.assertEqual(b21["base"], "600.00")
        self.assertEqual(b21["tax_amount"], "126.00")
        self.assertEqual(b10["base"], "400.00")
        self.assertEqual(b10["tax_amount"], "40.00")
        self.assertEqual(tax_amount, Decimal("166.00"))

    def test_two_configs_different_taxes_with_discount(self):
        """Discount factor applies proportionally to each tax's base."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("600.00"),
                          taxes=[(self.iva, Decimal("21.00"))])
        self._make_config(quote, Decimal("400.00"),
                          taxes=[(self.iva, Decimal("10.00"))])

        raw = Decimal("1000.00")
        discounted = Decimal("900.00")  # 10% discount
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        # discount_factor = 900/1000 = 0.9
        # IVA 21%: base = 600 * 0.9 = 540, tax = 113.40
        # IVA 10%: base = 400 * 0.9 = 360, tax = 36.00
        b21 = next(b for b in breakdown if b["percentage"] == "21.00")
        b10 = next(b for b in breakdown if b["percentage"] == "10.00")
        self.assertEqual(b21["base"], "540.00")
        self.assertEqual(b21["tax_amount"], "113.40")
        self.assertEqual(b10["base"], "360.00")
        self.assertEqual(b10["tax_amount"], "36.00")

    def test_inventory_item_taxes_aggregated(self):
        """Inventory item taxes are aggregated into quote-level taxes."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_inv_item(quote, Decimal("200.00"), Decimal("3"),
                            taxes=[(self.iva, Decimal("21.00"))])

        raw = Decimal("600.00")
        discounted = Decimal("600.00")
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        # base = 600 (3 * 200), tax = 600 * 21% = 126
        self.assertEqual(breakdown[0]["base"], "600.00")
        self.assertEqual(tax_amount, Decimal("126.00"))

    def test_config_and_inv_item_same_tax_aggregated(self):
        """Config and inv item with same tax type get bases aggregated."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("500.00"),
                          taxes=[(self.iva, Decimal("21.00"))])
        self._make_inv_item(quote, Decimal("100.00"), Decimal("5"),
                            taxes=[(self.iva, Decimal("21.00"))])

        raw = Decimal("1000.00")
        discounted = Decimal("1000.00")
        tax_amount, breakdown = QuoteTaxService.apply(quote, raw, discounted)

        # Aggregated base: 500 (config) + 500 (inv) = 1000
        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0]["base"], "1000.00")
        self.assertEqual(tax_amount, Decimal("210.00"))

    def test_retention_is_subtracted(self):
        """IVA + IRPF on one config: the retention lowers the total tax, its
        breakdown entry stays positive."""
        client = self._make_client()
        quote = self._make_quote(client)
        config = self._make_config(quote, Decimal("1000.00"), taxes=[
            (self.iva, Decimal("21.00")),
            (self.irpf, Decimal("15.00")),
        ])
        # Each tax on the config base (the helper's snapshot chains IRPF onto IVA).
        Configuration.objects.filter(pk=config.pk).update(tax_breakdown=[])

        tax_amount, breakdown = QuoteTaxService.apply(
            quote, Decimal("1000.00"), Decimal("900.00"),
        )

        # IVA: 900 * 21% = 189.00, IRPF: 900 * 15% = 135.00
        irpf_entry = next(b for b in breakdown if b["tax_name"] == "IRPF")
        self.assertEqual(irpf_entry["base"], "900.00")
        self.assertEqual(irpf_entry["tax_amount"], "135.00")
        self.assertEqual(tax_amount, Decimal("54.00"))

    def test_no_taxes_on_any_source(self):
        """If no source has taxes, quote should have no taxes."""
        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("1000.00"), taxes=[])

        tax_amount, breakdown = QuoteTaxService.apply(
            quote, Decimal("1000"), Decimal("1000"),
        )
        self.assertEqual(tax_amount, Decimal("0.00"))
        self.assertEqual(len(breakdown), 0)


# ---------------------------------------------------------------------------
# recalc_totals integration tests
# ---------------------------------------------------------------------------

class RecalcTotalsIntegrationTest(_QuoteTaxBase):
    """End-to-end tests that call quote.recalc_totals()."""

    def test_client_strategy_recalc(self):
        """recalc_totals uses ClientTaxStrategy when flag is ON and persists
        base_amount and calculated_amount on the quote's AppliedTax."""
        self.company.auto_apply_client_taxes = True
        self.company.save()

        client = self._make_client(taxes=[(self.iva, Decimal("21.00"))])
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("1000.00"),
                          taxes=[(self.iva, Decimal("21.00"))])

        quote.recalc_totals()
        quote.refresh_from_db()

        self.assertEqual(quote.subtotal_price.amount, Decimal("1000.00"))
        self.assertEqual(quote.discounted_subtotal.amount, Decimal("1000.00"))
        self.assertEqual(quote.tax_amount.amount, Decimal("210.00"))
        self.assertEqual(quote.total_price.amount, Decimal("1210.00"))
        self.assertEqual(quote.tax_breakdown[0]["base"], "1000.00")

        taxes = self._quote_taxes(quote)
        self.assertEqual(taxes.count(), 1)
        tax = taxes.first()
        self.assertEqual(tax.base_amount.amount, Decimal("1000.00"))
        self.assertEqual(tax.calculated_amount.amount, Decimal("210.00"))

    def test_source_strategy_recalc(self):
        """recalc_totals uses SourceTaxStrategy when flag is OFF."""
        self.company.auto_apply_client_taxes = False
        self.company.save()

        client = self._make_client()
        quote = self._make_quote(client)
        self._make_config(quote, Decimal("600.00"),
                          taxes=[(self.iva, Decimal("21.00"))])
        self._make_config(quote, Decimal("400.00"),
                          taxes=[(self.iva, Decimal("10.00"))])

        quote.recalc_totals()
        quote.refresh_from_db()

        self.assertEqual(quote.subtotal_price.amount, Decimal("1000.00"))
        # IVA 21% on 600 = 126, IVA 10% on 400 = 40 → total tax = 166
        self.assertEqual(quote.tax_amount.amount, Decimal("166.00"))
        self.assertEqual(quote.total_price.amount, Decimal("1166.00"))

    def test_recalc_subtracts_retention_from_total(self):
        """total_price = base + IVA - IRPF, like SalesInvoice.recalculate_totals."""
        self.company.auto_apply_client_taxes = False
        self.company.save()

        client = self._make_client()
        quote = self._make_quote(client)
        config = self._make_config(quote, Decimal("1000.00"), taxes=[
            (self.iva, Decimal("21.00")),
            (self.irpf, Decimal("15.00")),
        ])
        # Each tax on the config base (the helper's snapshot chains IRPF onto IVA).
        Configuration.objects.filter(pk=config.pk).update(tax_breakdown=[])

        quote.recalc_totals()
        quote.refresh_from_db()

        self.assertEqual(quote.tax_amount.amount, Decimal("60.00"))  # 210 - 150
        self.assertEqual(quote.total_price.amount, Decimal("1060.00"))
        irpf = self._quote_taxes(quote).get(tax_type=self.irpf)
        self.assertEqual(irpf.calculated_amount.amount, Decimal("150.00"))
