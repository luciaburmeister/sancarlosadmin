"""
Tests for the two-level discount system.

Covers:
- DiscountService.compute_effective_pct: combined percentage calculation
- Line-item discounts: rules with level=LINE_ITEM update QuoteInventoryItem.discount
- Basket discounts: rules with level=BASKET apply to the basket subtotal (unchanged)
- Mixed: both line-item and basket rules in the same pipeline
- Backwards compatibility: existing basket-only rules produce identical results
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ProductTemplate
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from sales.models import (
    Client,
    DiscountLevel,
    DiscountPolicy,
    DiscountRule,
    Quote,
    QuoteInventoryItem,
)
from sales.services.discount_service import DiscountService


# ---------------------------------------------------------------------------
# Unit tests for DiscountService
# ---------------------------------------------------------------------------

class TestDiscountServiceComputeEffectivePct(TestCase):
    """Unit tests for DiscountService.compute_effective_pct."""

    def _make_rule(self, name, pct, mode="net", condition="", order=0):
        """Create a lightweight rule-like object (no DB)."""

        class _Rule:
            pass

        r = _Rule()
        r.name = name
        r.percentage = Decimal(str(pct))
        r.mode = mode
        r.condition = condition
        r.order = order
        return r

    def test_single_net_rule(self):
        """A single net rule returns its percentage directly."""
        rules = [self._make_rule("10% off", 10)]
        pct, breakdown = DiscountService.compute_effective_pct(rules, {})
        self.assertEqual(pct, Decimal("10.00"))
        self.assertEqual(len(breakdown), 1)

    def test_two_net_rules(self):
        """Two net rules on a 100-base: 10% + 5% = 15%."""
        rules = [
            self._make_rule("R1", 10),
            self._make_rule("R2", 5),
        ]
        pct, _ = DiscountService.compute_effective_pct(rules, {})
        self.assertEqual(pct, Decimal("15.00"))

    def test_accumulated_rules(self):
        """Two accumulated rules: 10% then 5% on accumulated = 14.50%."""
        rules = [
            self._make_rule("R1", 10, mode="accumulated"),
            self._make_rule("R2", 5, mode="accumulated"),
        ]
        pct, _ = DiscountService.compute_effective_pct(rules, {})
        self.assertEqual(pct, Decimal("14.50"))

    def test_mixed_net_and_accumulated(self):
        """Net 10% then accumulated 5%: accumulated base = 90, 5% of 90 = 4.50, total = 14.50."""
        rules = [
            self._make_rule("R1", 10, mode="net"),
            self._make_rule("R2", 5, mode="accumulated"),
        ]
        pct, _ = DiscountService.compute_effective_pct(rules, {})
        self.assertEqual(pct, Decimal("14.50"))

    def test_condition_filters_rules(self):
        """Rules whose condition evaluates to False are skipped."""
        rules = [
            self._make_rule("Always", 10),
            self._make_rule("Big orders", 5, condition="subtotal > 5000"),
        ]
        ctx = {"subtotal": 1000}
        pct, breakdown = DiscountService.compute_effective_pct(rules, ctx)
        self.assertEqual(pct, Decimal("10.00"))
        self.assertEqual(len(breakdown), 1)

    def test_no_rules_returns_zero(self):
        pct, breakdown = DiscountService.compute_effective_pct([], {})
        self.assertEqual(pct, Decimal("0.00"))
        self.assertEqual(breakdown, [])

    def test_condition_sees_accumulated(self):
        """'accumulated' in a condition reflects the running total after prior rules."""
        rules = [
            self._make_rule("R1", 10),
            self._make_rule("R2", 5, condition="accumulated < 950"),
        ]
        # Subtotal 1000: after R1 the running total is 900, so R2 applies.
        _, breakdown = DiscountService.compute(Decimal("1000"), rules, {"subtotal": 1000})
        self.assertEqual(len(breakdown), 2)

        # Subtotal 1000 with R1 at 2%: running total 980, so R2 is skipped.
        rules[0].percentage = Decimal("2")
        _, breakdown = DiscountService.compute(Decimal("1000"), rules, {"subtotal": 1000})
        self.assertEqual(len(breakdown), 1)

    def test_effective_pct_condition_sees_accumulated_in_money(self):
        """With ``base`` given, 'accumulated' is the running money total, not a
        percentage out of 100, so tier thresholds in euros work."""
        rules = [
            self._make_rule("Descuento 35", 35),
            self._make_rule("Tramo 3%", 3, mode="accumulated",
                            condition="accumulated >= 5000 and accumulated <= 10000"),
        ]
        # Base 8000: after 35% the running total is 5200 → tramo applies.
        # Effective = 35 + 3% of the remaining 65 = 36.95.
        pct, breakdown = DiscountService.compute_effective_pct(
            rules, {"subtotal": 8000}, base=Decimal("8000"),
        )
        self.assertEqual(pct, Decimal("36.95"))
        self.assertEqual(len(breakdown), 2)

        # Base 6000: after 35% the running total is 3900 → tramo skipped.
        pct, breakdown = DiscountService.compute_effective_pct(
            rules, {"subtotal": 6000}, base=Decimal("6000"),
        )
        self.assertEqual(pct, Decimal("35.00"))
        self.assertEqual(len(breakdown), 1)

    def test_effective_pct_without_base_keeps_percentage_cascade(self):
        """Without ``base`` the cascade still runs on 100 and the rates are exact."""
        rules = [
            self._make_rule("R1", 35),
            self._make_rule("R2", 3, mode="accumulated"),
        ]
        pct, _ = DiscountService.compute_effective_pct(rules, {})
        self.assertEqual(pct, Decimal("36.95"))


# ---------------------------------------------------------------------------
# Integration tests for the two-phase pipeline
# ---------------------------------------------------------------------------

class _DiscountPipelineBase(TestCase):
    """Shared fixtures for discount pipeline tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Discount Test Co")
        cls.product = ProductTemplate.objects.create(
            name="Test Product", company=cls.company,
        )
        cls.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA",
            country="ES",
            defaults={
                "behavior": "add",
                "percentage_options": [0, 10, 21],
                "default_percentage": 21,
            },
        )

    def _make_client(self, name="Discount Client"):
        return Client.objects.create(name=name, company=self.company)

    def _make_quote(self, client=None):
        return Quote.objects.create(
            client=client or self._make_client(),
            company=self.company,
            project_name="Discount Pipeline Test",
        )

    def _make_inv_item(self, quote, unit_price, quantity, name="Test Item"):
        inv = InventoryItem.objects.create(
            name=name,
            company=self.company,
            selling_price=unit_price,
        )
        return QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=inv,
            quantity=quantity,
            unit_price=unit_price,
        )

    def _make_policy(self, rules_spec, assign_to_quote=None, assign_to_client=None, name="Test Policy"):
        """Create a DiscountPolicy with rules.

        rules_spec: list of dicts with keys: name, percentage, level,
                    and optionally: mode, condition, order.
        """
        policy = DiscountPolicy.objects.create(
            name=name, company=self.company, is_active=True,
        )
        for i, spec in enumerate(rules_spec):
            DiscountRule.objects.create(
                policy=policy,
                name=spec["name"],
                percentage=Decimal(str(spec["percentage"])),
                level=spec.get("level", DiscountLevel.BASKET),
                mode=spec.get("mode", "net"),
                condition=spec.get("condition", ""),
                order=spec.get("order", i),
                is_active=True,
            )
        if assign_to_quote:
            assign_to_quote.discount_policies.add(policy)
        if assign_to_client:
            assign_to_client.discount_policies.add(policy)
        return policy


class TestLineItemDiscounts(_DiscountPipelineBase):
    """Line-item discount rules set QuoteInventoryItem.discount."""

    def test_line_item_rule_writes_discount_to_items(self):
        """A 10% line-item rule sets discount=10 on each QuoteInventoryItem."""
        quote = self._make_quote()
        item1 = self._make_inv_item(quote, Decimal("100.00"), Decimal("2"))
        item2 = self._make_inv_item(quote, Decimal("50.00"), Decimal("4"), name="Item 2")

        self._make_policy(
            [{"name": "10% line", "percentage": 10, "level": DiscountLevel.LINE_ITEM}],
            assign_to_quote=quote,
        )

        quote.recalc_totals()

        item1.refresh_from_db()
        item2.refresh_from_db()
        self.assertEqual(item1.discount, Decimal("10.00"))
        self.assertEqual(item2.discount, Decimal("10.00"))

        # line_total = unit_price * qty * (1 - 0.10)
        self.assertEqual(item1.line_total.amount, Decimal("180.00"))  # 100*2*0.9
        self.assertEqual(item2.line_total.amount, Decimal("180.00"))  # 50*4*0.9

    def test_line_item_discount_reduces_subtotal(self):
        """Subtotal is computed from discounted line items."""
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("100.00"), Decimal("1"))

        self._make_policy(
            [{"name": "20% line", "percentage": 20, "level": DiscountLevel.LINE_ITEM}],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        # subtotal should be 80 (100 * 1 * 0.8)
        self.assertEqual(quote.subtotal_price.amount, Decimal("80.00"))

    def test_no_line_item_rules_leaves_discount_untouched(self):
        """If there are no line-item rules, existing per-item discounts are preserved."""
        quote = self._make_quote()
        item = self._make_inv_item(quote, Decimal("100.00"), Decimal("1"))

        # Manually set a per-item discount
        QuoteInventoryItem.objects.filter(pk=item.pk).update(discount=Decimal("5.00"))

        # Only basket-level rule
        self._make_policy(
            [{"name": "3% basket", "percentage": 3, "level": DiscountLevel.BASKET}],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        item.refresh_from_db()

        # The manual 5% should remain
        self.assertEqual(item.discount, Decimal("5.00"))

    def test_line_item_tier_condition_uses_money_accumulated(self):
        """A line-item tier rule conditioned on ``accumulated`` in euros applies
        exactly like the same rule at basket level (regression: it used to see
        the 100-unit percentage base and never fire)."""
        rules = [
            {"name": "Descuento 35", "percentage": 35, "level": DiscountLevel.LINE_ITEM, "order": 0},
            {"name": "Tramo 3%", "percentage": 3, "level": DiscountLevel.LINE_ITEM, "order": 1,
             "mode": "accumulated", "condition": "accumulated >= 5000 and accumulated <= 10000"},
        ]

        # 8000 € of items: 35% leaves 5200 → within the tier → 36.95 % effective.
        quote = self._make_quote()
        item = self._make_inv_item(quote, Decimal("1000.00"), Decimal("8"))
        self._make_policy(rules, assign_to_quote=quote)
        quote.recalc_totals()
        item.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(item.discount, Decimal("36.95"))
        self.assertEqual(quote.subtotal_price.amount, Decimal("5044.00"))  # 8000 * (1 - 0.3695)
        self.assertEqual(
            [e["rule_name"] for e in quote.discount_breakdown],
            ["Descuento 35", "Tramo 3%"],
        )

        # 6000 € of items: 35% leaves 3900 → below the tier → only 35 %.
        quote = self._make_quote()
        item = self._make_inv_item(quote, Decimal("1000.00"), Decimal("6"))
        self._make_policy(rules, assign_to_quote=quote)
        quote.recalc_totals()
        item.refresh_from_db()
        self.assertEqual(item.discount, Decimal("35.00"))


class TestBasketDiscounts(_DiscountPipelineBase):
    """Basket-level discount rules work exactly as before."""

    def test_basket_rule_applies_to_subtotal(self):
        """A 10% basket rule reduces the subtotal by 10%."""
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("200.00"), Decimal("1"))

        self._make_policy(
            [{"name": "10% basket", "percentage": 10, "level": DiscountLevel.BASKET}],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        self.assertEqual(quote.subtotal_price.amount, Decimal("200.00"))
        self.assertEqual(quote.discount_amount.amount, Decimal("20.00"))
        self.assertEqual(quote.discounted_subtotal.amount, Decimal("180.00"))

    def test_backwards_compat_default_level_is_basket(self):
        """Rules without an explicit level default to BASKET."""
        rule = DiscountRule(
            name="legacy",
            percentage=Decimal("5"),
        )
        self.assertEqual(rule.level, DiscountLevel.BASKET)


class TestMixedDiscounts(_DiscountPipelineBase):
    """Both line-item and basket rules in the same pipeline."""

    def test_line_item_then_basket(self):
        """
        Item: 100 EUR × 1
        Line-item rule: 10% → item becomes 90
        Basket rule: 5% on 90 → 4.50
        discounted_subtotal = 90 - 4.50 = 85.50
        """
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("100.00"), Decimal("1"))

        self._make_policy(
            [
                {"name": "10% line", "percentage": 10, "level": DiscountLevel.LINE_ITEM, "order": 0},
                {"name": "5% basket", "percentage": 5, "level": DiscountLevel.BASKET, "order": 1},
            ],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        # subtotal_price reflects the discounted item total
        self.assertEqual(quote.subtotal_price.amount, Decimal("90.00"))
        # basket discount is 5% of 90
        self.assertEqual(quote.discount_amount.amount, Decimal("4.50"))
        self.assertEqual(quote.discounted_subtotal.amount, Decimal("85.50"))

    def test_breakdown_contains_both_levels(self):
        """discount_breakdown has entries tagged with 'line_item' and 'basket'."""
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("100.00"), Decimal("1"))

        self._make_policy(
            [
                {"name": "Line", "percentage": 10, "level": DiscountLevel.LINE_ITEM},
                {"name": "Basket", "percentage": 5, "level": DiscountLevel.BASKET},
            ],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        levels = [e["level"] for e in quote.discount_breakdown]
        self.assertIn("line_item", levels)
        self.assertIn("basket", levels)


class TestMultiplePolicyOrdering(_DiscountPipelineBase):
    """Rules are applied one policy at a time; ``order`` never interleaves
    across policies."""

    def test_policies_run_one_after_another(self):
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("1000.00"), Decimal("1"))

        # Rule orders are chosen so that a global sort by ``order`` would
        # interleave the two policies (A1, B1, B2, A2).
        self._make_policy(
            [
                {"name": "A1", "percentage": 10, "order": 0},
                {"name": "A2", "percentage": 5, "order": 9, "mode": "accumulated"},
            ],
            assign_to_quote=quote, name="A policy",
        )
        self._make_policy(
            [
                {"name": "B1", "percentage": 2, "order": 1, "mode": "accumulated"},
                {"name": "B2", "percentage": 1, "order": 2, "mode": "accumulated"},
            ],
            assign_to_quote=quote, name="B policy",
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        self.assertEqual(
            [e["rule_name"] for e in quote.discount_breakdown],
            ["A1", "A2", "B1", "B2"],
        )
        # A1: 10% of 1000 = 100 → 900; A2: 5% of 900 = 45 → 855;
        # B1: 2% of 855 = 17.10 → 837.90; B2: 1% of 837.90 = 8.38
        self.assertEqual(quote.discount_amount.amount, Decimal("170.48"))

    def test_quote_policy_runs_before_client_policy(self):
        client = self._make_client()
        quote = self._make_quote(client=client)
        self._make_inv_item(quote, Decimal("1000.00"), Decimal("1"))

        # Client policy sorts first by name and by rule order, but the quote's
        # own policy has precedence and must run first.
        self._make_policy(
            [{"name": "Client rule", "percentage": 5, "order": 0}],
            assign_to_client=client, name="A client policy",
        )
        self._make_policy(
            [{"name": "Quote rule", "percentage": 10, "order": 5}],
            assign_to_quote=quote, name="Z quote policy",
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        self.assertEqual(
            [e["rule_name"] for e in quote.discount_breakdown],
            ["Quote rule", "Client rule"],
        )


class TestLineItemsBasketDiscounts(_DiscountPipelineBase):
    """LINE_ITEMS_BASKET rules apply only to the line items subtotal, excluding configurations."""

    def _make_config(self, quote, subtotal):
        return Configuration.objects.create(
            company=self.company,
            client=quote.client,
            quote=quote,
            product=self.product,
            name=f"Config {subtotal}",
            subtotal_price=subtotal,
            total_price=subtotal,
        )

    def test_excludes_configurations_from_base(self):
        """A 10% line-items-basket rule discounts only the inventory item subtotal,
        leaving the configuration subtotal untouched."""
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("200.00"), Decimal("1"))
        self._make_config(quote, Decimal("300.00"))

        self._make_policy(
            [{"name": "10% line items basket", "percentage": 10, "level": DiscountLevel.LINE_ITEMS_BASKET}],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        # Overall subtotal is unaffected: 200 (items) + 300 (config) = 500
        self.assertEqual(quote.subtotal_price.amount, Decimal("500.00"))
        # Discount is 10% of the 200 item subtotal only, not the 500 overall subtotal
        self.assertEqual(quote.discount_amount.amount, Decimal("20.00"))
        self.assertEqual(quote.discounted_subtotal.amount, Decimal("480.00"))

        levels = [e["level"] for e in quote.discount_breakdown]
        self.assertIn("line_items_basket", levels)

    def test_combines_with_basket_level(self):
        """LINE_ITEMS_BASKET and BASKET discounts both apply and add up."""
        quote = self._make_quote()
        self._make_inv_item(quote, Decimal("200.00"), Decimal("1"))
        self._make_config(quote, Decimal("300.00"))

        self._make_policy(
            [
                {"name": "10% line items basket", "percentage": 10, "level": DiscountLevel.LINE_ITEMS_BASKET, "order": 0},
                {"name": "5% basket", "percentage": 5, "level": DiscountLevel.BASKET, "order": 1},
            ],
            assign_to_quote=quote,
        )

        quote.recalc_totals()
        quote.refresh_from_db()

        # line_items_basket: 10% of 200 = 20.00
        # basket: 5% of the overall 500 subtotal = 25.00 (independent base, as with existing basket rules)
        self.assertEqual(quote.discount_amount.amount, Decimal("45.00"))
        self.assertEqual(quote.discounted_subtotal.amount, Decimal("455.00"))
