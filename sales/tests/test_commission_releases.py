"""
Commission releases: a quote's commission is released as the client pays the
invoices that bill it, advances included.
"""
from datetime import date
from io import StringIO
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase
from djmoney.money import Money

from configurator.commission.models import (
    AgentCommissionRule,
    CommissionRelease,
    CommissionReleaseStatus,
    CommissionRuleType,
    InvoiceCommission,
    QuoteCommission,
)
from configurator.commission.services import CommissionService
from invoices.models import SalesInvoiceStatus
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentMethod, PaymentStatus
from sales.models import Client, CommissionReleasePolicy, Quote, QuoteStatus
from sales.tests.factories import make_agent, make_quote


class _CommissionReleaseTestCase(AdvanceExpedienteMixin, TestCase):
    """An expediente with one accepted 1,000.00 quote and a 10 % agent: 100.00 of commission."""

    def setUp(self):
        super().setUp()
        self.agent = make_agent(company=self.company, name="Agente")
        self.rule = AgentCommissionRule.objects.create(
            company=self.company, agent=self.agent,
            rule_type=CommissionRuleType.PERCENTAGE, value=Decimal("10"),
        )
        self.commission = self.attach_agent(self.quote)

    def attach_agent(self, quote):
        Quote.objects.filter(pk=quote.pk).update(agent=self.agent)
        quote.refresh_from_db()
        CommissionService.prepopulate_for_quote(quote)
        CommissionService.recalculate(quote)
        return QuoteCommission.objects.get(quote=quote, agent=self.agent)

    def final_goods_invoice(self, deductions=()):
        invoice = self.invoice_goods()
        for advance_line, amount in deductions:
            self.deduct(invoice, advance_line, amount)
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        return invoice

    def pay(self, invoice, amount, payment_date=date(2026, 10, 1)):
        payment = Payment.objects.create(
            company=self.company,
            payment_date=payment_date,
            amount=Money(amount, "EUR"),
            direction=PaymentDirection.INBOUND,
            method=PaymentMethod.CASH,
            status=PaymentStatus.CONFIRMED,
        )
        return PaymentAllocation.objects.create(
            payment=payment,
            amount_applied=Money(amount, "EUR"),
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
        )

    def released(self, commission=None):
        return (commission or self.commission).released_amount.amount


class ReleaseOnPaymentTest(_CommissionReleaseTestCase):
    def test_nothing_is_released_before_the_client_pays(self):
        self.final_goods_invoice()
        self.assertEqual(self.released(), Decimal("0.00"))
        self.assertEqual(self.commission.release_status, CommissionReleaseStatus.NONE)

    def test_partial_payment_releases_its_share(self):
        invoice = self.final_goods_invoice()  # 1,210.00
        self.pay(invoice, Decimal("605.00"), date(2026, 10, 5))

        release = CommissionRelease.objects.get()
        self.assertEqual(release.released_amount.amount, Decimal("50.00"))
        self.assertEqual(release.base_amount.amount, Decimal("500.00"))
        self.assertEqual(release.release_date, date(2026, 10, 5))
        self.assertEqual(self.commission.pending_amount.amount, Decimal("50.00"))
        self.assertEqual(self.commission.release_status, CommissionReleaseStatus.PARTIAL)

    def test_full_payment_releases_the_whole_commission(self):
        # The client's own agent rule must not add to a fully quoted invoice
        Client.objects.filter(pk=self.client_obj.pk).update(agent=self.agent)
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("605.00"))
        self.pay(invoice, Decimal("605.00"))
        self.assertEqual(self.released(), Decimal("100.00"))
        self.assertEqual(self.commission.release_status, CommissionReleaseStatus.FULL)
        # The commissions-list row adds up the quote commission only
        row = InvoiceCommission.objects.get(sales_invoice=invoice)
        self.assertEqual(row.own_base_amount.amount, Decimal("0.00"))
        self.assertEqual(row.released_amount.amount, Decimal("100.00"))

    def test_removing_the_payment_takes_the_release_back(self):
        invoice = self.final_goods_invoice()
        allocation = self.pay(invoice, Decimal("1210.00"))
        allocation.delete()
        self.assertFalse(CommissionRelease.objects.exists())

        allocation = self.pay(invoice, Decimal("1210.00"))
        allocation.payment.delete()
        self.assertFalse(CommissionRelease.objects.exists())

    def test_cents_add_up_over_uneven_payments(self):
        invoice = self.final_goods_invoice()
        for amount in ("403.33", "403.33", "403.34"):
            self.pay(invoice, Decimal(amount))
        self.assertEqual(self.released(), Decimal("100.00"))

    def test_cancelled_invoice_releases_nothing(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"))
        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)
        self.assertEqual(self.released(), Decimal("0.00"))

    def test_recalculated_commission_updates_what_was_released(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("605.00"))
        self.rule.value = Decimal("20")
        self.rule.save()
        CommissionService.recalculate(self.quote)
        self.assertEqual(self.released(), Decimal("100.00"))


class AdvanceReleaseTest(_CommissionReleaseTestCase):
    def test_paid_advance_releases_and_its_deduction_keeps_the_total_exact(self):
        advance, advance_line = self.make_advance(Decimal("300.00"), "30")  # 363.00
        self.pay(advance, Decimal("363.00"))
        self.assertEqual(self.released(), Decimal("30.00"))

        invoice = self.final_goods_invoice(deductions=[(advance_line, Decimal("300.00"))])
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))
        self.pay(invoice, Decimal("847.00"))
        self.assertEqual(self.released(), Decimal("100.00"))

    def test_advance_is_split_across_the_expediente_quotes(self):
        second = make_quote(
            company=self.company, client=self.client_obj, project_name="Ampliación",
            project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )
        self.add_config(second, Decimal("500.00"))
        second_commission = self.attach_agent(second)

        advance, _line = self.make_advance(Decimal("450.00"), "30")  # 30 % of 1,500
        self.pay(advance, advance.total_amount.amount)

        self.assertEqual(self.released(), Decimal("30.00"))
        self.assertEqual(self.released(second_commission), Decimal("15.00"))


class ReleaseOnInvoiceTest(_CommissionReleaseTestCase):
    def setUp(self):
        super().setUp()
        self.agent.release_policy = CommissionReleasePolicy.ON_INVOICE
        self.agent.save()

    def test_issuing_the_invoice_releases_it_in_full(self):
        invoice = self.invoice_goods()
        self.assertEqual(self.released(), Decimal("0.00"))

        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        release = CommissionRelease.objects.get()
        self.assertIsNone(release.allocation)
        self.assertEqual(release.released_amount.amount, Decimal("100.00"))

        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)
        self.assertFalse(CommissionRelease.objects.exists())


class SyncCommandTest(_CommissionReleaseTestCase):
    def test_backfills_releases_of_already_paid_invoices(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"))
        CommissionRelease.objects.all().delete()

        call_command("sync_commission_releases", stdout=StringIO())
        self.assertEqual(self.released(), Decimal("100.00"))
