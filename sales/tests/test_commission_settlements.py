"""
Commission settlements: what an agent was paid, so each settlement lists only
what is still pending, whatever the date it was released on.

    python manage.py test sales.tests.test_commission_settlements
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import CompanyUser
from configurator.commission.models import (
    CommissionSettlement,
    CommissionSettlementLine,
    CommissionSettlementStatus,
    InvoiceCommission,
)
from configurator.commission.services import CommissionReleaseService, CommissionSettlementService
from configurator.services.export.commissionist_agent import CommissionistAgentExportStrategy, SettlementScope
from invoices.models import SalesInvoiceStatus
from sales.tests.factories import make_agent, make_company
from sales.tests.test_commission_releases import _CommissionReleaseTestCase

User = get_user_model()

AUGUST_END = date(2026, 8, 31)
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))
OCTOBER_END = date(2026, 10, 31)
NOVEMBER_END = date(2026, 11, 30)


class _SettlementTestCase(_CommissionReleaseTestCase):
    """The 1,210.00 invoice of 29/09/2026 carries 100.00 of the 10 % agent's commission."""

    def settle(self, until, agent=None):
        return CommissionSettlementService.settle(CommissionSettlement(agent=agent or self.agent, settled_until=until))

    def second_invoice(self):
        """A second expediente of the agent, invoiced and paid on 01/10/2026: another 100.00."""
        project_file, quote, _config = self.make_expediente("Segunda obra")
        self.attach_agent(quote)
        invoice = self.invoice_goods(project_file=project_file)
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        self.pay(invoice, invoice.total_amount.amount, date(2026, 10, 1))
        return invoice

    def pending(self, until, agent=None):
        return [
            (invoice.pk, invoice.pending_base, invoice.pending_commission, invoice.released_on)
            for invoice in CommissionSettlementService.pending_invoices(agent or self.agent, until)
        ]


class UnsettledTest(SimpleTestCase):
    """CommissionSettlementService.unsettled(released up to the cut-off, released by now, settled)."""

    def test_settlements_pay_the_earliest_releases_first(self):
        unsettled = CommissionSettlementService.unsettled
        cases = {
            "nothing settled": ((50, 100, 0), 50),
            "settled up to the cut-off": ((50, 100, 50), 0),
            "a later settlement also paid these": ((50, 100, 100), 0),
            "half of it settled": ((100, 100, 50), 50),
            "released after the cut-off only": ((0, 100, 0), 0),
            "settled release taken back": ((0, 0, 100), -100),
            "credit note not settled": ((-20, -20, 0), -20),
            "credit note settled after the cut-off": ((0, -20, -20), 0),
            "settled credit note cancelled": ((0, 0, -20), 20),
        }
        for name, (amounts, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(unsettled(*(Decimal(amount) for amount in amounts)), Decimal(expected))


class PendingSettlementTest(_SettlementTestCase):
    def test_release_dated_in_a_month_already_settled_is_still_pending(self):
        # August was settled before the invoice existed; it was paid in August
        CommissionSettlement.objects.create(agent=self.agent, settled_until=AUGUST_END)
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 8, 20))

        september = CommissionistAgentExportStrategy(self.agent, *SEPTEMBER).get_context()["settlement"]
        self.assertEqual(september["rows"], [])
        self.assertEqual(
            self.pending(SEPTEMBER[1]),
            [(invoice.pk, Decimal("1000.00"), Decimal("100.00"), date(2026, 8, 20))],
        )

    def test_settled_commission_is_not_pending_again(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("605.00"), date(2026, 10, 5))
        settlement = self.settle(OCTOBER_END)
        self.pay(invoice, Decimal("605.00"), date(2026, 11, 3))

        [line] = settlement.lines.all()
        self.assertEqual((line.base_amount.amount, line.commission_amount.amount, line.commission_rate),
                         (Decimal("500.00"), Decimal("50.00"), Decimal("10.00")))
        self.assertEqual(self.pending(OCTOBER_END), [])
        self.assertEqual(
            self.pending(NOVEMBER_END), [(invoice.pk, Decimal("500.00"), Decimal("50.00"), date(2026, 11, 3))],
        )
        self.assertEqual(InvoiceCommission.objects.with_settled().get().pending_settlement_total, Decimal("50.00"))

    def test_rebuilding_the_releases_keeps_what_was_settled(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        self.settle(OCTOBER_END)

        CommissionReleaseService.sync_for_invoice(invoice)

        self.assertEqual(self.pending(OCTOBER_END), [])
        self.assertEqual(InvoiceCommission.objects.get().settled_amount.amount, Decimal("100.00"))

    def test_settled_release_taken_back_is_pending_as_a_negative(self):
        invoice = self.final_goods_invoice()
        allocation = self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        self.settle(OCTOBER_END)

        allocation.delete()

        self.assertEqual(self.pending(OCTOBER_END), [(invoice.pk, Decimal("-1000.00"), Decimal("-100.00"), None)])

    def test_cancelled_invoice_already_settled_is_taken_back(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        self.settle(OCTOBER_END)

        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        self.assertFalse(InvoiceCommission.objects.exists())
        self.assertEqual(self.pending(OCTOBER_END), [(invoice.pk, Decimal("-1000.00"), Decimal("-100.00"), None)])

    def test_cancelled_settlement_is_pending_again(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        settlement = self.settle(OCTOBER_END)

        settlement.cancel()

        self.assertEqual(settlement.status, CommissionSettlementStatus.CANCELLED)
        self.assertEqual(
            self.pending(OCTOBER_END), [(invoice.pk, Decimal("1000.00"), Decimal("100.00"), date(2026, 10, 1))],
        )
        with self.assertRaises(ValidationError):
            settlement.cancel()

    def test_another_agents_settlement_pays_nothing_of_this_agents(self):
        other = make_agent(company=self.company, name="Otro")
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        CommissionSettlementLine.objects.create(
            settlement=CommissionSettlement.objects.create(agent=other, settled_until=OCTOBER_END),
            sales_invoice=invoice, commission_amount=Decimal("100.00"),
        )

        self.assertEqual(len(self.pending(OCTOBER_END)), 1)


class SettleTest(_SettlementTestCase):
    def test_nothing_pending_is_refused(self):
        with self.assertRaises(ValidationError):
            self.settle(OCTOBER_END)

        self.assertFalse(CommissionSettlement.objects.exists())

    def test_settlement_belongs_to_its_agents_company(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))

        self.assertEqual(self.settle(OCTOBER_END).company, self.company)
        with self.assertRaises(ValidationError) as raised:
            CommissionSettlement(
                agent=self.agent, company=make_company(name="Other"), settled_until=OCTOBER_END,
            ).full_clean()
        self.assertIn("agent", raised.exception.message_dict)

    def test_cut_off_cannot_be_in_the_future(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 8, 20))

        with self.assertRaises(ValidationError) as raised:
            self.settle(date(2999, 1, 31))

        self.assertIn("settled_until", raised.exception.message_dict)
        self.assertFalse(CommissionSettlement.objects.exists())

    def test_chosen_commissions_are_settled_alone(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        other = self.second_invoice()

        chosen = InvoiceCommission.objects.filter(sales_invoice=invoice)
        [settlement] = CommissionSettlementService.settle_commissions(chosen)

        self.assertEqual(list(settlement.lines.values_list("sales_invoice", flat=True)), [invoice.pk])
        self.assertEqual([row[0] for row in self.pending(OCTOBER_END)], [other.pk])
        self.assertEqual(CommissionSettlementService.settle_commissions(chosen), [])

    def test_settling_up_to_an_earlier_date_after_choosing_settles_nothing_twice(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("605.00"), date(2026, 10, 5))
        self.pay(invoice, Decimal("605.00"), date(2026, 11, 3))
        CommissionSettlementService.settle_commissions(InvoiceCommission.objects.all())  # today: all 100.00

        self.assertEqual(self.pending(OCTOBER_END), [])
        with self.assertRaises(ValidationError):
            self.settle(OCTOBER_END)

    def test_commission_marked_as_not_settled_is_pending_again(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("1210.00"), date(2026, 10, 1))
        settlement = self.settle(OCTOBER_END)

        cancelled = CommissionSettlementLine.objects.active().for_commission(InvoiceCommission.objects.get()).cancel()

        self.assertEqual(cancelled, 1)
        self.assertEqual(self.pending(OCTOBER_END)[0][2], Decimal("100.00"))
        recorded = CommissionistAgentExportStrategy(self.agent, settlement=settlement).get_context()["settlement"]
        self.assertEqual(recorded["rows"], [])

    def test_settlement_report_prints_the_recorded_lines(self):
        invoice = self.final_goods_invoice()
        self.pay(invoice, Decimal("605.00"), date(2026, 10, 5))
        settlement = self.settle(OCTOBER_END)
        self.pay(invoice, Decimal("605.00"), date(2026, 10, 20))  # after it was settled

        recorded = CommissionistAgentExportStrategy(self.agent, settlement=settlement).get_context()["settlement"]
        pending = CommissionistAgentExportStrategy(
            self.agent, date(2026, 10, 1), OCTOBER_END, settlement_scope=SettlementScope.PENDING,
        ).get_context()["settlement"]

        self.assertEqual(
            (recorded["scope"], recorded["number"], recorded["cut_off"]), ("recorded", settlement.pk, OCTOBER_END),
        )
        self.assertEqual(
            [(row["paid_on"], row["commission"]) for row in recorded["rows"]], [(date(2026, 10, 5), Decimal("50.00"))],
        )
        self.assertEqual([(row["paid_on"], row["percentage"], row["commission"]) for row in pending["rows"]],
                         [(date(2026, 10, 20), Decimal("10.00"), Decimal("50.00"))])


class _SettlementAdminTestCase(_SettlementTestCase):
    permissions = ()

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)
        self.user = User.objects.create_user(username="settler", password="x", is_staff=True)
        CompanyUser.objects.create(user=self.user, company=self.company, is_active=True)
        self.user.user_permissions.add(*Permission.objects.filter(codename__in=self.permissions))
        self.client.force_login(self.user)
        self.invoice = self.final_goods_invoice()
        self.pay(self.invoice, Decimal("1210.00"), date(2026, 10, 1))
        self.foreign_agent = make_agent(company=make_company(name="Other"), name="Foreign")


class SettlementAdminTest(_SettlementAdminTestCase):
    permissions = ("add_commissionsettlement", "view_commissionsettlement", "view_commissionistagent")

    def add(self, agent, until="2026-10-31"):
        return self.client.post(reverse("admin:configurator_commissionsettlement_add"), {
            "agent": agent.pk, "settled_until": until, "notes": "",
        })

    def test_adding_a_settlement_settles_what_is_pending(self):
        response = self.add(self.agent)

        self.assertEqual(response.status_code, 302)
        settlement = CommissionSettlement.objects.get()
        self.assertEqual(settlement.created_by, self.user)
        self.assertEqual(list(settlement.lines.values_list("sales_invoice", flat=True)), [self.invoice.pk])

    def test_nothing_pending_and_another_companys_agent_are_rejected(self):
        self.add(self.agent)

        for agent in (self.agent, self.foreign_agent):
            with self.subTest(agent.name):
                response = self.add(agent)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["adminform"].form.errors)
        self.assertEqual(CommissionSettlement.objects.count(), 1)

    def test_agent_page_offers_settling_to_who_can_add_settlements(self):
        url = reverse("admin:sales_commissionistagent_change", args=[self.agent.pk])
        settle_url = reverse("admin:configurator_commissionsettlement_add")

        response = self.client.get(url)
        self.assertContains(response, "Liquidar comisiones")
        form = self.client.get(settle_url, {"agent": self.agent.pk}).context["adminform"].form
        self.assertEqual(form["agent"].initial, str(self.agent.pk))

        self.user.user_permissions.remove(Permission.objects.get(codename="add_commissionsettlement"))
        self.client.force_login(User.objects.get(pk=self.user.pk))
        self.assertNotContains(self.client.get(url), "Liquidar comisiones")

    def test_print_opens_the_agents_reports_on_the_settlement(self):
        settlement = self.settle(OCTOBER_END)

        response = self.client.get(
            reverse("admin:configurator_commissionsettlement_print_detail_action", args=[settlement.pk])
        )

        reports = reverse("admin:sales_commissionistagent_reports", args=[self.agent.pk])
        self.assertRedirects(response, f"{reports}?settlement={settlement.pk}", fetch_redirect_response=False)

    def test_cancelling_needs_the_change_permission_and_a_confirmation(self):
        settlement = self.settle(OCTOBER_END)
        url = reverse("admin:configurator_commissionsettlement_cancel_detail_action", args=[settlement.pk])

        self.assertEqual(self.client.post(url).status_code, 403)
        self.user.user_permissions.add(Permission.objects.get(codename="change_commissionsettlement"))
        self.user = User.objects.get(pk=self.user.pk)  # drop the cached permissions
        self.client.force_login(self.user)

        self.assertContains(self.client.get(url), "Cancelar liquidación")
        settlement.refresh_from_db()
        self.assertEqual(settlement.status, CommissionSettlementStatus.CONFIRMED)

        self.client.post(url)
        settlement.refresh_from_db()
        self.assertEqual(
            (settlement.status, settlement.cancelled_by), (CommissionSettlementStatus.CANCELLED, self.user),
        )


class CommissionsListSettlementTest(_SettlementAdminTestCase):
    permissions = ("view_invoicecommission", "add_commissionsettlement", "change_commissionsettlement")

    def detail_action(self, name):
        commission = InvoiceCommission.objects.get(sales_invoice=self.invoice)
        return reverse(f"admin:configurator_invoicecommission_{name}", args=[commission.pk])

    def test_selected_commissions_are_marked_as_settled(self):
        other = self.second_invoice()
        chosen = InvoiceCommission.objects.get(sales_invoice=self.invoice)

        response = self.client.post(reverse("admin:configurator_invoicecommission_changelist"), {
            "action": "mark_selected_as_settled", "_selected_action": [chosen.pk],
        }, follow=True)

        self.assertContains(response, "1 comisión liquidada.")
        self.assertEqual(
            list(CommissionSettlementLine.objects.values_list("sales_invoice", flat=True)), [self.invoice.pk],
        )
        self.assertEqual([row[0] for row in self.pending(OCTOBER_END)], [other.pk])

    def test_one_commission_is_marked_as_settled_and_back_after_a_confirmation(self):
        settle_url = self.detail_action("mark_settled_detail_action")
        commission = InvoiceCommission.objects.get(sales_invoice=self.invoice)
        change_page = self.client.get(reverse("admin:configurator_invoicecommission_change", args=[commission.pk]))
        self.assertContains(change_page, settle_url)

        self.assertContains(self.client.get(settle_url), "Marcar como liquidada")
        self.assertFalse(CommissionSettlement.objects.exists())
        self.client.post(settle_url)
        self.assertEqual(self.pending(OCTOBER_END), [])

        self.client.post(self.detail_action("mark_unsettled_detail_action"))
        self.assertEqual(self.pending(OCTOBER_END)[0][2], Decimal("100.00"))
        self.assertEqual(CommissionSettlementLine.objects.get().cancelled_by, self.user)

    def test_marking_needs_the_settlement_permissions(self):
        self.user.user_permissions.remove(*Permission.objects.filter(
            codename__in=["add_commissionsettlement", "change_commissionsettlement"],
        ))
        self.client.force_login(User.objects.get(pk=self.user.pk))

        for name in ("mark_settled_detail_action", "mark_unsettled_detail_action"):
            with self.subTest(name):
                self.assertEqual(self.client.post(self.detail_action(name)).status_code, 403)
        self.assertFalse(CommissionSettlement.objects.exists())

    def test_pending_filter_leaves_settled_commissions_out(self):
        url = reverse("admin:configurator_invoicecommission_changelist")
        self.assertEqual(self.client.get(url, {"settlement": "pending"}).context["cl"].result_count, 1)

        self.settle(OCTOBER_END)

        self.assertEqual(self.client.get(url, {"settlement": "pending"}).context["cl"].result_count, 0)
        self.assertEqual(self.client.get(url, {"settlement": "settled"}).context["cl"].result_count, 1)


class SettlementReportViewTest(_SettlementAdminTestCase):
    permissions = ("view_commissionistagent", "view_quote", "view_salesinvoice", "view_invoicecommission")

    def url(self):
        return reverse("admin:sales_commissionistagent_reports", args=[self.agent.pk])

    def test_print_action_opens_the_reports_on_the_settlement(self):
        settlement = self.settle(OCTOBER_END)

        form = self.client.get(self.url(), {"settlement": settlement.pk}).context["form"]

        self.assertEqual(form["settlement_scope"].initial, SettlementScope.RECORDED)
        self.assertEqual((form["settlement"].initial, form["date_to"].initial), (settlement, OCTOBER_END))

    def test_another_agents_settlement_cannot_be_printed(self):
        foreign = CommissionSettlement.objects.create(agent=self.foreign_agent, settled_until=OCTOBER_END)

        response = self.client.post(self.url(), {
            "template": "", "date_from": "2026-10-01", "date_to": "2026-10-31", "file_format": "pdf",
            "settlement_scope": SettlementScope.RECORDED, "settlement": foreign.pk,
        })

        self.assertEqual(response.status_code, 200)
        self.assertIn("settlement", response.context["form"].errors)
