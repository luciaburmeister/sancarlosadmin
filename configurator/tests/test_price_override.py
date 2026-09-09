"""Tests for Configuration.price_override, including a zero override.

A zero Money is falsy, so a `if config.price_override` check silently ignores
an explicit 0 and falls back to the pricing rules. These tests pin the
"0 is a real override" behaviour.
"""
from decimal import Decimal

from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from configurator.models import (
    Configuration,
    ConfigurationStatus,
    ProductTemplate,
)


class PriceOverrideTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Override Co")
        cls.product = ProductTemplate.objects.create(
            name="Override Product",
            company=cls.company,
        )

    def _make_config(self, price_override=None):
        return Configuration.objects.create(
            company=self.company,
            product=self.product,
            name="Config",
            status=ConfigurationStatus.DRAFT,
            price_override=price_override,
        )

    def test_zero_override_sets_subtotal_to_zero(self):
        config = self._make_config(price_override=Money(0, "EUR"))
        config.recalculate_price()
        config.refresh_from_db()

        self.assertEqual(config.subtotal_price.amount, Decimal("0.00"))
        self.assertEqual(config.total_price.amount, Decimal("0.00"))

    def test_zero_override_is_recorded_in_the_pricing_breakdown(self):
        config = self._make_config(price_override=Money(0, "EUR"))
        config.recalculate_price()
        config.refresh_from_db()

        lines = config.pricing_breakdown.get("lines", [])
        self.assertEqual([line["type"] for line in lines], ["override"])
        self.assertEqual(lines[0]["amount"], "0.00")

    def test_positive_override_still_wins(self):
        config = self._make_config(price_override=Money(Decimal("150.00"), "EUR"))
        config.recalculate_price()
        config.refresh_from_db()

        self.assertEqual(config.subtotal_price.amount, Decimal("150.00"))

    def test_no_override_falls_back_to_pricing_rules(self):
        config = self._make_config(price_override=None)
        config.recalculate_price()
        config.refresh_from_db()

        self.assertEqual(config.pricing_breakdown.get("lines"), [])
