"""Workers can undo their own stage finish briefly; supervisors can reopen any finished stage."""
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.active_company import ActiveCompany
from company.models import Company
from configurator.models import ConfigurationItem
from materials.models import Component, ComponentType
from production.constants import ProductionActions
from production.models import ProductionItem, ProductionItemStatus
from production.services.stages_to_sessions import StagesToSessionsService
from production.tests.factories import make_company, make_item, make_product, make_staff_employee, make_ticket

CUT = "corte"
ASSEMBLY = "montaje"
PERMS = ["view_productionticket", "change_productionitem"]


class StageUndoReopenTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Alpha", stages=[CUT, ASSEMBLY])
        product = make_product(cls.company, name="Ventana", stages=[CUT, ASSEMBLY])
        cls.ticket = make_ticket(cls.company, stages=[CUT, ASSEMBLY], product=product, bom=False)
        cls.config = cls.ticket.source_object
        cls.item = make_item(
            cls.ticket,
            stages=[CUT, ASSEMBLY],
            planning={
                CUT: {"status": ProductionItemStatus.PENDING},
                ASSEMBLY: {"status": ProductionItemStatus.PENDING},
            },
        )
        cls.worker = cls._employee("worker")
        cls.other_worker = cls._employee("other")
        cls.supervisor = cls._employee("supervisor", is_production_supervisor=True)

    @classmethod
    def _employee(cls, username, **extra):
        return make_staff_employee(
            cls.company, username=username, perms=PERMS, stages=[CUT, ASSEMBLY], **extra
        )

    def setUp(self):
        cache.clear()
        self.addCleanup(ActiveCompany.clear_current)
        self.item.refresh_from_db()
        StagesToSessionsService.start_activity(stage_name=CUT, items=[self.item], employee=self.worker)
        StagesToSessionsService.finish_activity(stage_name=CUT, items=[self.item], employee=self.worker)
        self.item.refresh_from_db()

    def _cut(self):
        self.item.refresh_from_db()
        return self.item.planning[CUT]

    def _post(self, employee, action, **data):
        self.client.force_login(employee.user)
        self.client.post(
            reverse("admin:company_switch_active_apply"), {"companies": [self.company.pk]}
        )
        return self.client.post(
            reverse("admin:production_productionticket_stage_action", args=[self.ticket.pk, CUT]),
            {"action": action, "item_ids": str(self.item.pk), **data},
        )

    def test_worker_undoes_own_finish_within_window(self):
        self._post(self.worker, ProductionActions.UNDO_FINISH)

        cut = self._cut()
        self.assertEqual(cut["status"], ProductionItemStatus.IN_PROGRESS)
        self.assertIsNone(cut["completed_at"])
        self.assertEqual(cut["revert_log"][-1]["action"], ProductionActions.UNDO_FINISH)
        self.assertEqual(cut["revert_log"][-1]["employee_id"], self.worker.pk)

        # The stage can be finished again afterwards.
        StagesToSessionsService.start_session(stage_name=CUT, items=[self.item], employee=self.worker)
        StagesToSessionsService.finish_activity(stage_name=CUT, items=[self.item], employee=self.worker)
        self.assertEqual(self._cut()["status"], ProductionItemStatus.DONE)

    def test_undo_is_refused_after_the_window(self):
        planning = self.item.planning
        planning[CUT]["completed_at"] = (
            self.item.updated_at - timedelta(minutes=self.company.production_stage_undo_minutes + 1)
        ).isoformat()
        ProductionItem.objects.filter(pk=self.item.pk).update(planning=planning)
        self.item.refresh_from_db()

        with self.assertRaises(ValidationError):
            StagesToSessionsService.undo_finish(stage_name=CUT, items=[self.item], employee=self.worker)
        self.assertEqual(self._cut()["status"], ProductionItemStatus.DONE)

    def test_undo_is_refused_when_disabled_for_the_company(self):
        Company.objects.filter(pk=self.company.pk).update(production_stage_undo_minutes=0)
        self.item.refresh_from_db()

        self.assertFalse(self.item.can_undo_stage_finish(CUT, self.worker))

    def test_worker_cannot_undo_someone_elses_finish(self):
        self._post(self.other_worker, ProductionActions.UNDO_FINISH)

        self.assertEqual(self._cut()["status"], ProductionItemStatus.DONE)

    def test_worker_cannot_reopen(self):
        self._post(self.other_worker, ProductionActions.REOPEN_STAGE, target=ProductionItemStatus.PENDING)

        self.assertEqual(self._cut()["status"], ProductionItemStatus.DONE)

    def test_supervisor_reopens_to_pending(self):
        self._post(self.supervisor, ProductionActions.REOPEN_STAGE, target=ProductionItemStatus.PENDING)

        cut = self._cut()
        self.assertEqual(cut["status"], ProductionItemStatus.PENDING)
        self.assertIsNone(cut["started_at"])
        self.assertEqual(cut["revert_log"][-1]["user_id"], self.supervisor.user_id)

    def test_reopen_is_refused_once_a_later_stage_started(self):
        StagesToSessionsService.start_activity(
            stage_name=ASSEMBLY, items=[self.item], employee=self.other_worker
        )

        self._post(self.supervisor, ProductionActions.REOPEN_STAGE, target=ProductionItemStatus.IN_PROGRESS)

        self.assertEqual(self._cut()["status"], ProductionItemStatus.DONE)

    def test_stages_page_offers_undo_to_the_finishing_worker_only(self):
        def finished_cut_units(employee):
            self.client.force_login(employee.user)
            self.client.post(
                reverse("admin:company_switch_active_apply"), {"companies": [self.company.pk]}
            )
            response = self.client.get(
                reverse("admin:production_productionticket_stages", args=[self.ticket.pk])
            )
            return [
                unit
                for stage in response.context["stages_ui"] if stage["stage_name"] == CUT
                for unit in stage["units"]
            ]

        [unit] = finished_cut_units(self.worker)
        self.assertTrue(unit["can_undo"])
        self.assertEqual(finished_cut_units(self.other_worker), [])
        [unit] = finished_cut_units(self.supervisor)
        self.assertTrue(unit["can_reopen"])

    def test_api_revert_to_pending_requires_a_supervisor(self):
        # The terminal API only exposes items linked to a current configuration line.
        component = Component.objects.create(
            name="Perfil", company=self.company, component_type=ComponentType.STANDARD
        )
        config_item = ConfigurationItem.objects.create(
            configuration=self.config, component=component, quantity=1, unit_price=0
        )
        self.item.data = {"configuration_item_id": config_item.pk}
        self.item.save(update_fields=["data"])
        client = APIClient()
        client.force_authenticate(user=self.worker.user)
        url = reverse(
            "stage_revert_item", args=[self.config.pk, self.item.pk, CUT]
        )

        response = client.post(url, {"target": ProductionItemStatus.PENDING}, format="json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._cut()["status"], ProductionItemStatus.DONE)
