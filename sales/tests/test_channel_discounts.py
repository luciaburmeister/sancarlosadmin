from decimal import Decimal

from django.test import TestCase

from sales.models import DiscountLevel, DiscountMode
from sales.services.channel_discounts import ChannelDiscountService, VolumeTier
from sales.tests.factories import make_client, make_company, make_discount_policy


class ChannelDiscountServiceTests(TestCase):
    TIERS = (
        {"name": "Tramo 2%", "percentage": 2, "mode": DiscountMode.ACCUMULATED, "level": DiscountLevel.LINE_ITEMS_BASKET,
         "condition": "accumulated >= 2500 and accumulated <= 5000"},
        {"name": "Tramo 3%", "percentage": 3, "mode": DiscountMode.ACCUMULATED, "level": DiscountLevel.LINE_ITEMS_BASKET,
         "condition": "accumulated >= 5000 and accumulated <= 10000"},
        {"name": "Tramo 5%", "percentage": 5, "mode": DiscountMode.ACCUMULATED, "level": DiscountLevel.LINE_ITEMS_BASKET,
         "condition": "accumulated >= 10000"},
    )

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.distributor = make_discount_policy(
            company=cls.company,
            name="Distribuidor Grande",
            rules=({"name": "Descuento 35", "percentage": 35, "level": DiscountLevel.LINE_ITEM}, *cls.TIERS),
        )
        cls.client_ = make_client(company=cls.company)
        cls.client_.discount_policies.add(cls.distributor)

    def policy(self, *rules: dict, **kwargs) -> list:
        return [make_discount_policy(company=self.company, rules=rules, **kwargs)]

    def test_base_discount_and_volume_tiers(self):
        discount = ChannelDiscountService.for_client(self.company, self.client_)

        self.assertTrue(discount.is_supported)
        self.assertEqual(discount.product_percentage, Decimal("35.00"))
        self.assertEqual(
            discount.tiers,
            (
                VolumeTier(Decimal("2500"), Decimal("2.00")),
                VolumeTier(Decimal("5000"), Decimal("3.00")),
                VolumeTier(Decimal("10000"), Decimal("5.00")),
            ),
        )

    def test_product_rules_combine_as_on_a_quote(self):
        discount = ChannelDiscountService.for_policies(self.policy(
            {"name": "Base", "percentage": 20, "level": DiscountLevel.LINE_ITEM},
            {"name": "Extra", "percentage": 10, "level": DiscountLevel.LINE_ITEM, "mode": DiscountMode.ACCUMULATED},
        ))

        self.assertEqual(discount.product_percentage, Decimal("28.00"))
        self.assertEqual(discount.tiers, ())

    def test_company_auto_applied_policy_follows_the_clients_own(self):
        make_discount_policy(
            company=self.company, name="Web", auto_apply=True,
            rules=({"name": "Web 10", "percentage": 10, "level": DiscountLevel.LINE_ITEM,
                    "mode": DiscountMode.ACCUMULATED},),
        )

        discount = ChannelDiscountService.for_client(self.company, self.client_)

        self.assertEqual(discount.product_percentage, Decimal("41.50"))  # 35 %, then 10 % of the rest

    def test_client_without_policies_gets_nothing(self):
        discount = ChannelDiscountService.for_client(self.company, make_client(company=self.company, name="Retail"))

        self.assertTrue(discount.is_supported)
        self.assertTrue(discount.is_empty)

    def test_inactive_policies_and_rules_are_ignored(self):
        client = make_client(company=self.company, name="Paused")
        client.discount_policies.add(
            *self.policy({"name": "Off", "percentage": 50, "level": DiscountLevel.LINE_ITEM}, is_active=False),
            *self.policy({"name": "Rule off", "percentage": 50, "level": DiscountLevel.LINE_ITEM, "is_active": False}),
        )

        self.assertTrue(ChannelDiscountService.for_client(self.company, client).is_empty)

    def test_reversed_and_chained_comparisons(self):
        discount = ChannelDiscountService.for_policies(self.policy(
            {"name": "Low", "percentage": 2, "level": DiscountLevel.BASKET, "condition": "1000 <= accumulated < 2000"},
            {"name": "High", "percentage": 4, "level": DiscountLevel.BASKET, "condition": "2000 < accumulated"},
        ))

        self.assertEqual(
            discount.tiers, (VolumeTier(Decimal("1000"), Decimal("2.00")), VolumeTier(Decimal("2000"), Decimal("4.00")))
        )

    def test_conditions_a_channel_cannot_follow_are_not_offered(self):
        cases = {
            "conditional product discount": {"name": "Big", "percentage": 5, "level": DiscountLevel.LINE_ITEM,
                                             "condition": "subtotal > 1000"},
            "condition on the subtotal before discounts": {"name": "Big", "percentage": 5,
                                                           "level": DiscountLevel.BASKET, "condition": "subtotal > 1000"},
            "condition on the quantity": {"name": "Big", "percentage": 5, "level": DiscountLevel.BASKET,
                                          "condition": "accumulated > 1000 and total_quantity > 10"},
        }
        for case, rule in cases.items():
            with self.subTest(case):
                discount = ChannelDiscountService.for_policies(self.policy(rule))

                self.assertFalse(discount.is_supported)
                self.assertIn("Big", discount.unsupported)

    def test_tiers_the_channel_would_apply_differently_are_not_offered(self):
        cases = {
            "gap between tiers": ("accumulated >= 1000 and accumulated < 2000", "accumulated >= 3000"),
            "overlapping tiers": ("accumulated >= 1000", "accumulated >= 3000"),
            "highest tier has an upper limit": ("accumulated >= 1000 and accumulated < 2000",
                                                "accumulated >= 2000 and accumulated < 3000"),
        }
        for case, (low, high) in cases.items():
            with self.subTest(case):
                discount = ChannelDiscountService.for_policies(self.policy(
                    {"name": "Low", "percentage": 2, "level": DiscountLevel.BASKET, "condition": low},
                    {"name": "High", "percentage": 4, "level": DiscountLevel.BASKET, "condition": high},
                ))

                self.assertFalse(discount.is_supported)

        with self.subTest("larger amount, smaller discount"):
            discount = ChannelDiscountService.for_policies(self.policy(
                {"name": "Low", "percentage": 4, "level": DiscountLevel.BASKET,
                 "condition": "accumulated >= 1000 and accumulated < 2000"},
                {"name": "High", "percentage": 2, "level": DiscountLevel.BASKET, "condition": "accumulated >= 2000"},
            ))

            self.assertFalse(discount.is_supported)

    def test_order_discount_without_condition_is_a_tier_from_zero(self):
        always = {"name": "Always", "percentage": 2, "level": DiscountLevel.LINE_ITEMS_BASKET}

        alone = ChannelDiscountService.for_policies(self.policy(always))
        with_tiers = ChannelDiscountService.for_policies(self.policy(always, *self.TIERS))

        self.assertEqual(alone.tiers, (VolumeTier(Decimal("0"), Decimal("2.00")),))
        self.assertFalse(with_tiers.is_supported)  # the ERP stacks it on the tiers; a channel picks one

    def test_surcharges_are_not_offered(self):
        discount = ChannelDiscountService.for_policies(self.policy(
            {"name": "Surcharge", "percentage": -5, "level": DiscountLevel.LINE_ITEM},
        ))

        self.assertFalse(discount.is_supported)
