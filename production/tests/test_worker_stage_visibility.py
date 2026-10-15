"""Shop-floor workers only see production tickets and stages assigned to them."""

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from company.active_company import ActiveCompany
from production.constants import ProductionActions
from production.models import (
    AssemblyLine,
    AssemblyLineStatus,
    ProductionItemSession,
    ProductionItemStatus,
    ProductionTicket,
)
from production.tests.factories import (
    make_company,
    make_item,
    make_product,
    make_staff_employee,
    make_staff_user,
    make_ticket,
)

CUT = "corte"
ASSEMBLY = "montaje"
PERMS = ["view_productionticket", "change_productionitem"]


class WorkerStageVisibilityTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Alpha", stages=[CUT, ASSEMBLY])
        cls.other_company = make_company(name="Beta", stages=[CUT, ASSEMBLY])

        cls.cut_ready = cls._ticket(cls.company, cut=ProductionItemStatus.PENDING)
        cls.assembly_ready = cls._ticket(cls.company, cut=ProductionItemStatus.DONE)
        cls.finished = cls._ticket(
            cls.company, cut=ProductionItemStatus.DONE, assembly=ProductionItemStatus.DONE
        )
        cls._ticket(cls.other_company, cut=ProductionItemStatus.PENDING)

        cls.cutter = make_staff_employee(
            cls.company, username="cutter", perms=PERMS, stages=[CUT]
        ).user
        cls.unassigned = make_staff_employee(
            cls.company, username="unassigned", perms=PERMS, stages=[]
        ).user
        cls.office = make_staff_user(username="office", companies=[cls.company], perms=PERMS)

    @staticmethod
    def _ticket(company, cut, assembly=ProductionItemStatus.PENDING):
        product = make_product(company, name="Ventana", stages=[CUT, ASSEMBLY])
        ticket = make_ticket(company, stages=[CUT, ASSEMBLY], product=product, bom=False)
        make_item(ticket, stages=[CUT, ASSEMBLY], planning={CUT: {"status": cut}, ASSEMBLY: {"status": assembly}})
        ticket.refresh_from_db()
        return ticket

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
