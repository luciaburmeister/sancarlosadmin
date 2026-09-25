"""The pricing rule admin lists every variable a rule expression can use.

Configuration.get_pricing_context() exposes the built-in aggregates plus all
ProductParameter and ProductComputedVariable names of the product template,
so the admin reference panel must show the same set, scoped to the rule's
company.
"""
import time

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import Company
from configurator.admin.pricing_rule_template import PricingRuleAdmin
from configurator.models import (
    ParameterType,
    PricingRule,
    ProductComputedVariable,
    ProductParameter,
    ProductTemplate,
)


class PricingRuleAdminAvailableVariablesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Rules Co")
        cls.other_company = Company.objects.create(name="Other Co")

        cls.product = ProductTemplate.objects.create(name="Window", company=cls.company)
        ProductParameter.objects.create(
            product=cls.product, name="width", label="Width", type=ParameterType.NUMBER
        )
        ProductComputedVariable.objects.create(
            product=cls.product, company=cls.company, name="area", expression="width * 2"
        )

        cls.other_product = ProductTemplate.objects.create(
            name="Door", company=cls.other_company
        )
        ProductComputedVariable.objects.create(
            product=cls.other_product,
            company=cls.other_company,
            name="secret_var",
            expression="1",
        )

        cls.rule = PricingRule.objects.create(
            company=cls.company, name="Margin", expression="base_total * 1.1"
        )

    def setUp(self):
        # Not in setUpTestData: Django deep-copies those attributes per test
        # and a ModelAdmin instance is not copyable.
        self.admin = PricingRuleAdmin(PricingRule, AdminSite())
        # LocMem cache persists across tests in-process; a stale cached list
        # of switchable company ids would reject this test's companies.
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)

    def test_lists_builtin_parameters_and_computed_variables(self):
        html = str(self.admin.available_variables(self.rule))

        for name in ("base_total", "material_cost", "quantity", "item_count"):
            self.assertIn(f"<code class=\"font-mono text-xs\">{name}</code>", html)
        self.assertIn("width", html)
        self.assertIn("area", html)
        self.assertIn("width * 2", html)
        self.assertIn("Window", html)

    def test_scoped_to_the_rule_company(self):
        html = str(self.admin.available_variables(self.rule))

        self.assertNotIn("secret_var", html)
        self.assertNotIn("Door", html)

    def test_unsaved_rule_outside_request_shows_all_products(self):
        html = str(self.admin.available_variables(None))

        self.assertIn("area", html)
        self.assertIn("secret_var", html)

    def test_products_without_variables_are_omitted(self):
        ProductTemplate.objects.create(name="Empty", company=self.company)

        html = str(self.admin.available_variables(self.rule))

        self.assertNotIn("Empty", html)

    def _login_with_active_company(self, username, company):
        user = get_user_model().objects.create_superuser(
            username=username, email=f"{username}@r.com", password="x"
        )
        self.client.force_login(user)
        # The switcher defaults to the first company by name, which would be
        # "Other Co"; pin the active company to the rule's company instead.
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
        session.save()

    def test_change_page_renders_the_panel(self):
        self._login_with_active_company("rules_admin", self.company)

        resp = self.client.get(
            reverse("admin:configurator_pricingrule_change", args=[self.rule.pk])
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("area", body)
        self.assertIn("width * 2", body)
        self.assertNotIn("secret_var", body)

    def test_add_page_shows_active_company_products_only(self):
        self._login_with_active_company("rules_admin2", self.company)

        resp = self.client.get(reverse("admin:configurator_pricingrule_add"))

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("base_total", body)
        self.assertIn("area", body)
        self.assertNotIn("secret_var", body)
