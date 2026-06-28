"""
Tests for CommissionistAgent model, Client agent FK, commission rules,
QuoteCommission, and CommissionService.
"""
from decimal import Decimal
from django.test import TestCase
from django.db import IntegrityError
from django.contrib.contenttypes.models import ContentType

from company.models import Company
from sales.models import CommissionistAgent, Client, Quote
from configurator.models import ProductTemplate, Configuration
from configurator.commission.models import (
    AgentCommissionRule, QuoteCommission,
    CommissionRuleType,
)
from configurator.commission.services import CommissionService
from invoices.models import TaxType, AppliedTax


class _BaseTestCase(TestCase):
    """Shared fixtures for commissionist tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")

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
        product_ct = ContentType.objects.get_for_model(cls.product)
        AppliedTax.objects.create(
            content_type=product_ct,
            object_id=cls.product.pk,
            tax_type=cls.vat_21,
            selected_percentage=Decimal("21.00"),
            order=0,
        )


# ---------------------------------------------------------------------------
# CommissionistAgent Model
# ---------------------------------------------------------------------------

class CommissionistAgentModelTest(_BaseTestCase):
    """Test CommissionistAgent model creation and fields."""

    def test_create_agent(self):
        agent = CommissionistAgent.objects.create(
            name="Juan Pérez",
            company=self.company,
            tax_identifier="12345678A",
            email="juan@example.com",
            phone="+34666111222",
            commission_percentage=Decimal("5.00"),
        )
        self.assertEqual(agent.name, "Juan Pérez")
        self.assertEqual(agent.commission_percentage, Decimal("5.00"))
        self.assertTrue(agent.is_active)
        self.assertEqual(str(agent), "Juan Pérez")

    def test_agent_is_company_owned(self):
        agent = CommissionistAgent.objects.create(
            name="Agent A",
            company=self.company,
        )
        self.assertEqual(agent.company, self.company)

    def test_agent_defaults(self):
        agent = CommissionistAgent.objects.create(
            name="Agent B",
            company=self.company,
        )
        self.assertEqual(agent.commission_percentage, Decimal("0"))
        self.assertTrue(agent.is_active)
        self.assertEqual(agent.email, "")
        self.assertEqual(agent.phone, "")
        self.assertEqual(agent.tax_identifier, "")


# ---------------------------------------------------------------------------
# Client.agent FK
# ---------------------------------------------------------------------------

class ClientAgentFKTest(_BaseTestCase):
    """Test that Client.agent points to CommissionistAgent."""

    def test_client_agent_fk(self):
        agent = CommissionistAgent.objects.create(
            name="Agent X",
            company=self.company,
            commission_percentage=Decimal("3.50"),
        )
        client = Client.objects.create(
            name="Client Corp",
            company=self.company,
            agent=agent,
        )
        client.refresh_from_db()
        self.assertEqual(client.agent, agent)
        self.assertEqual(client.agent.commission_percentage, Decimal("3.50"))

    def test_client_agent_nullable(self):
        client = Client.objects.create(
            name="Clientless Corp",
            company=self.company,
        )
        self.assertIsNone(client.agent)

    def test_agent_deletion_sets_null(self):
        agent = CommissionistAgent.objects.create(
            name="Temp Agent",
            company=self.company,
        )
        client = Client.objects.create(
            name="Some Client",
            company=self.company,
            agent=agent,
        )
        agent.delete()
        client.refresh_from_db()
        self.assertIsNone(client.agent)


# ---------------------------------------------------------------------------
# AgentCommissionRule
# ---------------------------------------------------------------------------

class AgentCommissionRuleTest(_BaseTestCase):
    """Test AgentCommissionRule linked to CommissionistAgent."""

    def setUp(self):
        self.agent = CommissionistAgent.objects.create(
            name="Commission Agent",
            company=self.company,
            commission_percentage=Decimal("5.00"),
        )

    def test_create_percentage_rule(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("5.00"),
            company=self.company,
        )
        self.assertEqual(rule.agent, self.agent)
        self.assertEqual(rule.rule_type, CommissionRuleType.PERCENTAGE)
        self.assertEqual(rule.value, Decimal("5.00"))

    def test_create_fixed_rule(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.FIXED,
            value=Decimal("100.00"),
            company=self.company,
        )
        self.assertEqual(rule.rule_type, CommissionRuleType.FIXED)

    def test_compute_percentage_commission(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("10.00"),
            company=self.company,
        )
        result = rule.compute_commission(
            subtotal=Decimal("1000.00"),
            total=Decimal("1210.00"),
            quantity=Decimal("5"),
        )
        self.assertEqual(result, Decimal("100.00"))

    def test_compute_fixed_commission(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.FIXED,
            value=Decimal("250.00"),
            company=self.company,
        )
        result = rule.compute_commission(
            subtotal=Decimal("1000.00"),
            total=Decimal("1210.00"),
            quantity=Decimal("5"),
        )
        self.assertEqual(result, Decimal("250.00"))

    def test_unique_together_agent_product(self):
        AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("5.00"),
            company=self.company,
        )
        with self.assertRaises(IntegrityError):
            AgentCommissionRule.objects.create(
                agent=self.agent,
                product_template=self.product,
                rule_type=CommissionRuleType.FIXED,
                value=Decimal("10.00"),
                company=self.company,
            )

    def test_str_representation(self):
        rule = AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("5.00"),
            company=self.company,
        )
        self.assertIn("Commission Agent", str(rule))
        self.assertIn("Aluminium Window", str(rule))


# ---------------------------------------------------------------------------
# QuoteCommission
# ---------------------------------------------------------------------------

class QuoteCommissionTest(_BaseTestCase):
    """Test QuoteCommission linked to CommissionistAgent."""

    def setUp(self):
        self.agent = CommissionistAgent.objects.create(
            name="Quote Agent",
            company=self.company,
        )
        self.client = Client.objects.create(
            name="Test Client",
            company=self.company,
            agent=self.agent,
        )
        self.quote = Quote.objects.create(
            client=self.client,
            company=self.company,
            project_name="Test Project",
        )
        self.config = Configuration.objects.create(
            company=self.company,
            client=self.client,
            product=self.product,
            name="Config 1",
            quote=self.quote,
        )

    def test_create_quote_commission(self):
        qc = QuoteCommission.objects.create(
            quote=self.quote,
            configuration=self.config,
            agent=self.agent,
        )
        self.assertEqual(qc.agent, self.agent)
        self.assertEqual(qc.configuration, self.config)

    def test_quote_commission_agent_nullable(self):
        qc = QuoteCommission.objects.create(
            quote=self.quote,
            configuration=self.config,
            agent=None,
        )
        self.assertIsNone(qc.agent)


# ---------------------------------------------------------------------------
# CommissionService
# ---------------------------------------------------------------------------

class CommissionServiceTest(_BaseTestCase):
    """Test CommissionService with CommissionistAgent."""

    def setUp(self):
        self.agent = CommissionistAgent.objects.create(
            name="Service Agent",
            company=self.company,
            commission_percentage=Decimal("5.00"),
        )
        self.client = Client.objects.create(
            name="Service Client",
            company=self.company,
            agent=self.agent,
        )
        self.quote = Quote.objects.create(
            client=self.client,
            company=self.company,
            project_name="Service Project",
        )
        self.config = Configuration.objects.create(
            company=self.company,
            client=self.client,
            product=self.product,
            name="Config Service",
            quote=self.quote,
        )
        AgentCommissionRule.objects.create(
            agent=self.agent,
            product_template=self.product,
            rule_type=CommissionRuleType.PERCENTAGE,
            value=Decimal("5.00"),
            company=self.company,
        )

    def test_sync_creates_rows(self):
        CommissionService.prepopulate_for_quote(self.quote)
        qcs = QuoteCommission.objects.filter(quote=self.quote)
        self.assertEqual(qcs.count(), 1)
        self.assertIsNone(qcs.first().agent)

    def test_recalculate_with_agent(self):
        CommissionService.prepopulate_for_quote(self.quote)
        qc = QuoteCommission.objects.get(quote=self.quote, configuration=self.config)
        qc.agent = self.agent
        qc.save()

        CommissionService.recalculate(self.quote)
        qc.refresh_from_db()
        self.assertEqual(qc.rule_type_snapshot, CommissionRuleType.PERCENTAGE)

    def test_get_summary(self):
        CommissionService.prepopulate_for_quote(self.quote)
        qc = QuoteCommission.objects.get(quote=self.quote, configuration=self.config)
        qc.agent = self.agent
        qc.save()
        CommissionService.recalculate(self.quote)

        summary = CommissionService.get_commission_context(self.quote)
        self.assertTrue(summary["has_commissions"])
        self.assertEqual(len(summary["commissions"]), 1)
        self.assertEqual(summary["commissions"][0]["agent_name"], "Service Agent")
