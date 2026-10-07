"""A worker with an open session elsewhere is told which task blocks them."""
from datetime import date

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from company.models import Company, CompanyUser, Employee
from configurator.models import Configuration, ProductTemplate
from production.constants import ProductionActions
from production.exceptions import WorkerBusyError
from production.models import ProductionItem, ProductionItemStatus, ProductionTicket
from production.services.stages_to_sessions import StagesToSessionsService

User = get_user_model()

CUT = "corte"


class WorkerBusyTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Alpha", production_stages=[CUT])
        cls.other_company = Company.objects.create(name="Beta", production_stages=[CUT])
        permissions = Permission.objects.filter(
            codename__in=["view_productionticket", "change_productionitem"]
        )
        cls.busy_ticket, cls.busy_item = cls._ticket(cls.company)
        cls.ticket, cls.item = cls._ticket(cls.company)
        cls.worker = cls._employee("worker", cls.company, permissions)
        cls.other_ticket, cls.other_item = cls._ticket(cls.other_company)
        cls.other_worker = cls._employee("other", cls.other_company, permissions)

    @classmethod
    def _ticket(cls, company):
        product = ProductTemplate.objects.create(
            name="Ventana", company=company, production_stages=[CUT]
        )
        config = Configuration.objects.create(company=company, product=product)
        ticket = ProductionTicket.objects.create(
            company=company,
            content_type=ContentType.objects.get_for_model(config),
            object_id=config.id,
        )
        item = ProductionItem.objects.create(
            ticket=ticket,
            company=company,
            planning={CUT: {"status": ProductionItemStatus.PENDING}},
            production_stages_order=[CUT],
        )
        return ticket, item

    @classmethod
    def _employee(cls, username, company, permissions):
        user = User.objects.create_user(username=username, password="x", is_staff=True)
        user.user_permissions.set(permissions)
        CompanyUser.objects.create(user=user, company=company)
        return Employee.objects.create(
            company=company,
            user=user,
            first_name=username,
            last_name="Test",
            hired_at=date(2020, 1, 1),
            can_work_on_production=True,
            production_stages=[CUT],
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)
        StagesToSessionsService.start_activity(
            stage_name=CUT, items=[self.busy_item], employee=self.worker
        )
        StagesToSessionsService.start_activity(
            stage_name=CUT, items=[self.other_item], employee=self.other_worker
        )
        self.client.force_login(self.worker.user)
        self.client.post(
            reverse("admin:company_switch_active_apply"), {"companies": [self.company.pk]}
        )

    def _cut_status(self, item):
        item.refresh_from_db()
        return item.planning[CUT]["status"]

    def test_service_reports_the_blocking_session(self):
        with self.assertRaises(WorkerBusyError) as ctx:
            StagesToSessionsService.start_activity(
                stage_name=CUT, items=[self.item], employee=self.worker
            )

        session = ctx.exception.session
        self.assertEqual(session.production_item.ticket_id, self.busy_ticket.pk)
        self.assertEqual(session.production_stage, CUT)
        self.assertIn(f"#{self.busy_ticket.pk}", ctx.exception.message)
        self.assertEqual(self._cut_status(self.item), ProductionItemStatus.PENDING)

    def test_admin_start_shows_which_task_blocks_without_success(self):
        # Consume the company-switch confirmation so only this action's messages remain.
        self.client.get(reverse("admin:index"))
        response = self.client.post(
            reverse("admin:production_productionticket_stage_action", args=[self.ticket.pk, CUT]),
            {"action": ProductionActions.START_ACTIVITY, "item_ids": str(self.item.pk)},
            follow=True,
        )

        sent = list(response.context["messages"])
        errors = [str(m) for m in sent if m.level == messages.ERROR]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f"#{self.busy_ticket.pk}", errors[0])
        self.assertIn(CUT, errors[0])
        self.assertFalse([m for m in sent if m.level == messages.SUCCESS])
        self.assertEqual(self._cut_status(self.item), ProductionItemStatus.PENDING)

    def test_stages_page_shows_the_task_on_another_ticket(self):
        response = self.client.get(
            reverse("admin:production_productionticket_stages", args=[self.ticket.pk])
        )

        session = response.context["active_session_elsewhere"]
        self.assertEqual(session.production_item.ticket_id, self.busy_ticket.pk)
        self.assertContains(
            response,
            reverse("admin:production_productionticket_stages", args=[self.busy_ticket.pk]),
        )

    def test_stages_page_hides_banner_on_the_ticket_being_worked(self):
        response = self.client.get(
            reverse("admin:production_productionticket_stages", args=[self.busy_ticket.pk])
        )

        self.assertIsNone(response.context["active_session_elsewhere"])

    def test_other_company_worker_session_is_not_shown_or_blocking(self):
        StagesToSessionsService.leave_activity(
            stage_name=CUT, items=[self.busy_item], employee=self.worker
        )

        response = self.client.get(
            reverse("admin:production_productionticket_stages", args=[self.ticket.pk])
        )
        self.assertIsNone(response.context["active_session_elsewhere"])

        StagesToSessionsService.start_activity(
            stage_name=CUT, items=[self.item], employee=self.worker
        )
        self.assertEqual(self._cut_status(self.item), ProductionItemStatus.IN_PROGRESS)
