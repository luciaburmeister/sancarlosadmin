"""Shop-floor workers only see production tickets and stages assigned to them."""
from datetime import date

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
from production.models import (
    AssemblyLine,
    AssemblyLineStatus,
    ProductionItem,
    ProductionItemSession,
    ProductionItemStatus,
    ProductionTicket,
)

User = get_user_model()

CUT = "corte"
ASSEMBLY = "montaje"


class WorkerStageVisibilityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Alpha", production_stages=[CUT, ASSEMBLY])
        cls.other_company = Company.objects.create(name="Beta", production_stages=[CUT, ASSEMBLY])

        cls.cut_ready = cls._ticket(cls.company, cut=ProductionItemStatus.PENDING)
        cls.assembly_ready = cls._ticket(cls.company, cut=ProductionItemStatus.DONE)
        cls.finished = cls._ticket(
            cls.company, cut=ProductionItemStatus.DONE, assembly=ProductionItemStatus.DONE
        )
        cls._ticket(cls.other_company, cut=ProductionItemStatus.PENDING)

        permissions = Permission.objects.filter(
            codename__in=["view_productionticket", "change_productionitem"]
        )
        cls.cutter = cls._user("cutter", permissions, stages=[CUT])
        cls.unassigned = cls._user("unassigned", permissions, stages=[])
        cls.office = cls._user("office", permissions, stages=None)

    @classmethod
    def _ticket(cls, company, cut, assembly=ProductionItemStatus.PENDING):
        product = ProductTemplate.objects.create(
            name="Ventana", company=company, production_stages=[CUT, ASSEMBLY]
        )
        config = Configuration.objects.create(company=company, product=product)
        ticket = ProductionTicket.objects.create(
            company=company,
            content_type=ContentType.objects.get_for_model(config),
            object_id=config.id,
        )
        ProductionItem.objects.create(
            ticket=ticket,
            company=company,
            planning={CUT: {"status": cut}, ASSEMBLY: {"status": assembly}},
            production_stages_order=[CUT, ASSEMBLY],
        )
        ticket.refresh_from_db()
        return ticket

    @classmethod
    def _user(cls, username, permissions, stages):
        user = User.objects.create_user(username=username, password="x", is_staff=True)
        user.user_permissions.set(permissions)
        CompanyUser.objects.create(user=user, company=cls.company)
        if stages is not None:
            Employee.objects.create(
                company=cls.company,
                user=user,
                first_name=username,
                last_name="Test",
                hired_at=date(2020, 1, 1),
                can_work_on_production=True,
                production_stages=stages,
            )
        return user

    def setUp(self):
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)

    def _login(self, user):
        self.client.force_login(user)
        self.client.post(
            reverse("admin:company_switch_active_apply"), {"companies": [self.company.pk]}
        )

    def _listed_tickets(self, user):
        self._login(user)
        response = self.client.get(reverse("admin:production_productionticket_changelist"))
        self.assertEqual(response.status_code, 200)
        return set(response.context["cl"].queryset)

    def _visible_stages(self, user, ticket):
        self._login(user)
        response = self.client.get(
            reverse("admin:production_productionticket_stages", args=[ticket.pk])
        )
        self.assertEqual(response.status_code, 200)
        return [stage["stage_name"] for stage in response.context["stages_ui"]]

    def test_worker_lists_only_tickets_whose_current_stage_is_theirs(self):
        self.assertEqual(self._listed_tickets(self.cutter), {self.cut_ready})

    def test_worker_without_assigned_stages_lists_nothing(self):
        self.assertEqual(self._listed_tickets(self.unassigned), set())

    def test_office_user_lists_every_company_ticket(self):
        self.assertEqual(
            self._listed_tickets(self.office),
            {self.cut_ready, self.assembly_ready, self.finished},
        )

    def test_stages_page_shows_only_assigned_stages(self):
        self.assertEqual(self._visible_stages(self.cutter, self.cut_ready), [CUT])
        self.assertEqual(self._visible_stages(self.cutter, self.assembly_ready), [])
        self.assertEqual(self._visible_stages(self.unassigned, self.cut_ready), [])
        self.assertEqual(self._visible_stages(self.office, self.cut_ready), [CUT, ASSEMBLY])
        self.assertEqual(self._visible_stages(self.office, self.assembly_ready), [ASSEMBLY])

    def _start_stage(self, ticket, stage):
        item = ticket.items.get()
        self.client.post(
            reverse("admin:production_productionticket_stage_action", args=[ticket.pk, stage]),
            {"action": ProductionActions.START_ACTIVITY, "item_ids": str(item.pk)},
        )
        return ProductionItemSession.objects.filter(production_item=item, production_stage=stage)

    def test_worker_can_act_on_an_assigned_stage(self):
        self._login(self.cutter)

        self.assertTrue(self._start_stage(self.cut_ready, CUT).exists())

    def test_worker_cannot_act_on_an_unassigned_stage(self):
        self._login(self.cutter)

        self.assertFalse(self._start_stage(self.assembly_ready, ASSEMBLY).exists())
        item = self.assembly_ready.items.get()
        self.assertEqual(item.planning[ASSEMBLY]["status"], ProductionItemStatus.PENDING)

    def test_parallel_assembly_line_makes_every_unfinished_stage_ready(self):
        tickets = ProductionTicket.objects.filter(pk=self.cut_ready.pk)
        self.assertFalse(tickets.ready_for_stages({ASSEMBLY}).exists())

        line = AssemblyLine.objects.create(
            company=self.company,
            stages=[CUT, ASSEMBLY],
            parallel_stages=True,
            status=AssemblyLineStatus.IN_PROGRESS,
        )
        line.tickets.add(self.cut_ready)

        self.assertTrue(tickets.ready_for_stages({ASSEMBLY}).exists())
