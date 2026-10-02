"""The commission follows the quote's totals and the agent's rule wherever they change."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command

from configurator.commission.models import AgentCommissionRule, CommissionRuleType, InvoiceCommission, QuoteCommission
from sales.models import CommissionistAgent, Quote
from sales.tests.test_commission_releases import _CommissionReleaseTestCase


class CommissionRecalculationTest(_CommissionReleaseTestCase):
    def test_adding_a_configuration_updates_the_commission(self):
        self.add_config(self.quote, Decimal("500.00"))  # saved outside the quote admin
        self.commission.refresh_from_db()
        self.assertEqual(self.commission.commission_amount.amount, Decimal("150.00"))

    def test_rule_created_later_fills_quotes_calculated_without_one(self):
        agent = CommissionistAgent.objects.create(name="Nuevo", company=self.company)
        _pf, quote, _config = self.make_expediente("Otra obra")
        Quote.objects.filter(pk=quote.pk).update(agent=agent)
        quote.refresh_from_db()
        quote.recalc_totals()
        commission = QuoteCommission.objects.get(quote=quote, agent=agent)
        self.assertEqual(commission.commission_amount.amount, Decimal("0.00"))

        AgentCommissionRule.objects.create(
            company=self.company, agent=agent,
            rule_type=CommissionRuleType.PERCENTAGE, value=Decimal("5"),
        )
        commission.refresh_from_db()
        self.assertEqual(commission.commission_amount.amount, Decimal("50.00"))

    def test_changing_the_agent_moves_the_commission(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, invoice.total_amount.amount)
        other = CommissionistAgent.objects.create(name="Otro", company=self.company)
        Quote.objects.filter(pk=self.quote.pk).update(agent=other)
        self.quote.refresh_from_db()
        self.quote.recalc_totals()

        self.assertEqual(
            list(QuoteCommission.objects.filter(quote=self.quote).values_list("agent_id", flat=True)),
            [other.pk],
        )
        self.assertEqual(
            list(InvoiceCommission.objects.filter(sales_invoice=invoice).values_list("agent_id", flat=True)),
            [other.pk],
        )

    def test_command_recalculates_stale_commissions(self):
        QuoteCommission.objects.filter(pk=self.commission.pk).update(commission_amount=Decimal("1.00"))
        call_command("recalculate_commissions", stdout=StringIO())
        self.commission.refresh_from_db()
        self.assertEqual(self.commission.commission_amount.amount, Decimal("100.00"))
