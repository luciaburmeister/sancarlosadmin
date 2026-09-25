"""recalculate_price() records what every pricing rule produced and snapshots
the computed variables, so the admin can show both without re-evaluating."""
import time
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import translation
from djmoney.money import Money

from company.active_company import ActiveCompany
from company.models import Company
from configurator.admin.configuration import (
    ConfigurationAdmin,
    PricingRuleAssignmentInline,
)
from configurator.models import (
    Configuration,
    ConfigurationStatus,
    ParameterType,
    PricingRule,
    PricingRuleAssignment,
    ProductComputedVariable,
    ProductParameter,
    ProductTemplate,
)


class PricingRuleResultsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Results Co")
        cls.product = ProductTemplate.objects.create(name="Panel", company=cls.company)
        ProductParameter.objects.create(
            product=cls.product,
            name="width",
            label="Width",
            type=ParameterType.NUMBER,
            default_value="5",
        )
        ProductComputedVariable.objects.create(
            product=cls.product, company=cls.company, name="area", expression="width * 2"
        )
        ProductComputedVariable.objects.create(
            product=cls.product, company=cls.company, name="tag", expression="'x'"
        )

        def rule(name, expression, **kwargs):
            return PricingRule.objects.create(
                company=cls.company, name=name, expression=expression, **kwargs
            )

        cls.charge = rule("Charge", "10")
        cls.hidden_discount = rule("Hidden", "-3", show_in_export=False)
        cls.shown_discount = rule("Shown", "-2", show_in_export=True)
        cls.noop = rule("Noop", "0")
        cls.disabled = rule("Disabled", "100")
        cls.uses_var = rule("Area based", "area")

        cls.config = Configuration.objects.create(
            company=cls.company,
            product=cls.product,
            name="Config",
            status=ConfigurationStatus.DRAFT,
        )
        for r in (cls.charge, cls.hidden_discount, cls.shown_discount, cls.noop, cls.uses_var):
            PricingRuleAssignment.objects.create(configuration=cls.config, rule=r)
        PricingRuleAssignment.objects.create(
            configuration=cls.config, rule=cls.disabled, enabled=False
        )

    def setUp(self):
        # Status strings are asserted in English; the test process may run in es.
        translation.activate("en")
        self.addCleanup(translation.deactivate)
        self.config.recalculate_price()
        self.config.refresh_from_db()
        self.inline = PricingRuleAssignmentInline(Configuration, AdminSite())
        self.admin = ConfigurationAdmin(Configuration, AdminSite())

    def _result(self, rule):
        return next(
            r for r in self.config.pricing_breakdown["rules"] if r["rule_id"] == rule.pk
        )

    def test_computed_variables_are_snapshotted(self):
        self.assertEqual(self.config.computed_variables, {"area": 10, "tag": "x"})

    def test_every_enabled_rule_gets_a_result(self):
        ids = [r["rule_id"] for r in self.config.pricing_breakdown["rules"]]
        self.assertCountEqual(
            ids,
            [self.charge.pk, self.hidden_discount.pk, self.shown_discount.pk,
             self.noop.pk, self.uses_var.pk],
        )

    def test_results_carry_amount_and_applied_flag(self):
        self.assertEqual(self._result(self.charge)["amount"], "10.00")
        self.assertTrue(self._result(self.charge)["applied"])
        self.assertEqual(self._result(self.charge)["type"], "charge")

        self.assertEqual(self._result(self.uses_var)["amount"], "10.00")

        hidden = self._result(self.hidden_discount)
        self.assertEqual(hidden["amount"], "-3.00")
        self.assertTrue(hidden["applied"])
        self.assertFalse(hidden["show_in_export"])

        self.assertEqual(self._result(self.noop)["amount"], "0.00")
        self.assertFalse(self._result(self.noop)["applied"])

    def test_totals_unchanged_by_bookkeeping(self):
        # 0 + 10 + 10 (area) - 3 - 2
        self.assertEqual(self.config.subtotal_price.amount, Decimal("15.00"))
        lines = self.config.pricing_breakdown["lines"]
        self.assertEqual([l["type"] for l in lines], ["charge", "charge", "discount"])
        self.assertEqual(self.config.pricing_breakdown["discount_total"], "-5.00")

    def test_inline_shows_amount_and_result(self):
        by_rule = {a.rule_id: a for a in self.config.pricing_rules.all()}

        self.assertEqual(self.inline.evaluated_amount(by_rule[self.charge.pk]), "10.00 EUR")
        self.assertIn("Charge", str(self.inline.evaluation_status(by_rule[self.charge.pk])))
        self.assertIn(
            "hidden in export",
            str(self.inline.evaluation_status(by_rule[self.hidden_discount.pk])),
        )
        self.assertIn("No effect", str(self.inline.evaluation_status(by_rule[self.noop.pk])))
        self.assertEqual(self.inline.evaluated_amount(by_rule[self.disabled.pk]), "—")
        self.assertEqual(str(self.inline.evaluation_status(by_rule[self.disabled.pk])), "Disabled")

    def test_inline_with_price_override(self):
        self.config.price_override = Money(50, "EUR")
        self.config.save()
        self.config.recalculate_price()
        self.config.refresh_from_db()
        assignment = self.config.pricing_rules.get(rule=self.charge)

        self.assertEqual(self.config.pricing_breakdown["rules"], [])
        self.assertEqual(self.inline.evaluated_amount(assignment), "—")
        self.assertIn("override", str(self.inline.evaluation_status(assignment)))

    def test_change_page_shows_rule_amounts_and_computed_variables(self):
        user = get_user_model().objects.create_superuser(
            username="cfg_admin", email="c@c.com", password="x"
        )
        self.client.force_login(user)
        cache.clear()  # stale switchable-company ids from earlier tests
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()
        self.addCleanup(ActiveCompany.clear_current)

        resp = self.client.get(
            reverse("admin:configurator_configuration_change", args=[self.config.pk])
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("10.00 EUR", body)
        self.assertIn("-3.00 EUR", body)
        self.assertIn("area", body)

    def test_computed_variables_display(self):
        html = str(self.admin.computed_variables_display(self.config))

        self.assertIn("area", html)
        self.assertIn(">10<", html)
        self.assertIn("tag", html)

        empty = Configuration(product=self.product, company=self.company)
        self.assertEqual(str(self.admin.computed_variables_display(empty)), "Not calculated yet")
