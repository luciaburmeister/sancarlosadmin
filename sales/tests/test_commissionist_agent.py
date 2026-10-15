"""
Tests for Quote.agent auto-population, commission rule computation and
CommissionService.

Commissions operate at the quote/basket level — one rule per agent,
one QuoteCommission per (quote, agent).
"""
from decimal import Decimal
from django.test import TestCase
from django.db import IntegrityError

from configurator.models import ProductTemplate
from configurator.commission.models import (
    AgentCommissionRule, QuoteCommission,
    CommissionRuleType,
)
from configurator.commission.services import CommissionService
from invoices.models import TaxType
from sales.tests.factories import apply_taxes, make_agent, make_client, make_company, make_configuration, make_quote


class _BaseTestCase(TestCase):
    """Shared fixtures for commissionist tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Test Co")

        cls.vat_21, _ = TaxType.objects.get_or_create(
            tax_name="IVA",
            country="ES",
            defaults={
                "behavior": "add",
                "percentage_options": [0, 4, 10, 21],
                "default_percentage": 21,
            },
        )

        cls.product = ProductTemplate.objects.create(
            name="Aluminium Window",
            company=cls.company,
        )

        # Attach a default tax to the product
        apply_taxes(cls.product, taxes=[(cls.vat_21, Decimal("21.00"))])


# ---------------------------------------------------------------------------
# Quote.agent auto-population
# ---------------------------------------------------------------------------

class QuoteAgentAutoPopulationTest(_BaseTestCase):
    """Test that Quote.agent is auto-populated from Client.agent."""

    def test_auto_populate_agent_from_client(self):
        agent = make_agent(company=self.company, name="Client's Agent")
        client = make_client(company=self.company, name="Test Client", agent=agent)
        quote = make_quote(company=self.company, client=client, project_name="Auto Agent Test")
        quote.refresh_from_db()
        self.assertEqual(quote.agent, agent)

    def test_no_auto_populate_when_agent_set(self):
        agent1 = make_agent(company=self.company, name="Agent 1")
        agent2 = make_agent(company=self.company, name="Agent 2")
        client = make_client(company=self.company, name="Test Client", agent=agent1)
        quote = make_quote(company=self.company, client=client, project_name="Explicit Agent Test", agent=agent2)
        quote.refresh_from_db()
        self.assertEqual(quote.agent, agent2)

    def test_no_agent_when_client_has_none(self):
        client = make_client(company=self.company, name="No-Agent Client")
        quote = make_quote(company=self.company, client=client, project_name="No Agent Test")
        quote.refresh_from_db()
        self.assertIsNone(quote.agent)


# ---------------------------------------------------------------------------
# AgentCommissionRule (now per-agent, no product_template)
# ---------------------------------------------------------------------------

class AgentCommissionRuleTest(_BaseTestCase):
    """Test AgentCommissionRule linked to CommissionistAgent."""

    def setUp(self):
        self.agent = make_agent(company=self.company, name="Commission Agent", commission_percentage=Decimal("5.00"))

    def test_rule_value_is_formatted_per_rule_type(self):
        cases = [
            (CommissionRuleType.PERCENTAGE, Decimal("5.00"), "5.00%"),
            (CommissionRuleType.FIXED, Decimal("100"), "€100.00"),
        ]
        for rule_type, value, expected in cases:
            with self.subTest(rule_type=rule_type):
                rule = AgentCommissionRule(agent=self.agent, rule_type=rule_type, value=value)
                self.assertEqual(rule.format_value(), expected)
                self.assertEqual(str(rule), f"Commission Agent [{expected}]")

    def test_compute_percentage_commission(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("10.00"),
            company=self.company,
        )
        result = rule.compute_commission(
            subtotal=Decimal("1000.00"),
            discounted_subtotal=Decimal("900.00"),
            total=Decimal("1210.00"),
            quantity=Decimal("5"),
        )
        # On the base after discounts
        self.assertEqual(result, Decimal("90.00"))

    def test_compute_fixed_commission(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            rule_type=CommissionRuleType.FIXED,
            value=Decimal("250.00"),
            company=self.company,
        )
        result = rule.compute_commission(
            subtotal=Decimal("1000.00"),
            discounted_subtotal=Decimal("900.00"),
            total=Decimal("1210.00"),
            quantity=Decimal("5"),
        )
        self.assertEqual(result, Decimal("250.00"))

    def test_unique_per_agent(self):
        AgentCommissionRule.objects.create(
            agent=self.agent,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("5.00"),
            company=self.company,
        )
        with self.assertRaises(IntegrityError):
            AgentCommissionRule.objects.create(
                agent=self.agent,
                rule_type=CommissionRuleType.FIXED,
                value=Decimal("10.00"),
                company=self.company,
            )


# ---------------------------------------------------------------------------
# CommissionService (basket-level)
# ---------------------------------------------------------------------------

class CommissionServiceTest(_BaseTestCase):
    """Test CommissionService with basket-level commission calculation."""

    def setUp(self):
        self.agent = make_agent(company=self.company, name="Service Agent", commission_percentage=Decimal("5.00"))
        self.client = make_client(company=self.company, name="Service Client", agent=self.agent)
        self.quote = make_quote(company=self.company, client=self.client, project_name="Service Project")
        self.config = make_configuration(quote=self.quote, product=self.product, name="Config Service")
        AgentCommissionRule.objects.create(
            agent=self.agent,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("5.00"),
            company=self.company,
        )

    def test_prepopulate_creates_row_with_agent(self):
        CommissionService.prepopulate_for_quote(self.quote)
        qcs = QuoteCommission.objects.filter(quote=self.quote)
        self.assertEqual(qcs.count(), 1)
        # Agent is pre-filled from quote.agent
        self.assertEqual(qcs.first().agent, self.agent)

    def test_prepopulate_no_row_when_no_agent(self):
        # Remove the client's agent so auto-populate doesn't re-set it
        self.client.agent = None
        self.client.save(update_fields=["agent"])
        self.quote.agent = None
        self.quote.save()
        # Clear any rows from prior state
        QuoteCommission.objects.filter(quote=self.quote).delete()
        CommissionService.prepopulate_for_quote(self.quote)
        qcs = QuoteCommission.objects.filter(quote=self.quote)
        self.assertEqual(qcs.count(), 0)

    def test_recalculate_with_agent(self):
        CommissionService.prepopulate_for_quote(self.quote)
        CommissionService.recalculate(self.quote)
        qc = QuoteCommission.objects.get(quote=self.quote, agent=self.agent)
        self.assertEqual(qc.rule_type_snapshot, CommissionRuleType.PERCENTAGE)

    def test_get_summary(self):
        CommissionService.prepopulate_for_quote(self.quote)
        CommissionService.recalculate(self.quote)

        summary = CommissionService.get_commission_context(self.quote)
        self.assertTrue(summary["has_commissions"])
        self.assertEqual(len(summary["commissions"]), 1)
        self.assertEqual(summary["commissions"][0]["agent_name"], "Service Agent")
