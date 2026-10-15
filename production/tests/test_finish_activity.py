from django.test import TestCase
from django.utils import timezone

from production.exceptions import StageHasActiveWorkersError
from production.models import (
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
)
from production.services.stages_to_sessions import StagesToSessionsService
from production.tests.factories import make_company, make_employee, make_item, make_product, make_ticket


class FinishActivitySharedWorkersTest(TestCase):
    STAGE = "limpieza"

    def setUp(self):
        self.company = make_company(
            stages=[self.STAGE],
            allow_shared_production_stage_activities=True,
        )
        product = make_product(self.company, name="Wash product", stages=[self.STAGE])
        self.ticket = make_ticket(self.company, stages=[self.STAGE], product=product, bom=False)
        self.item = make_item(self.ticket, stages=[self.STAGE])
        self.jose = make_employee(self.company, first_name="Jose", last_name="Murillo", stages=[])
        self.maria = make_employee(self.company, first_name="Maria", last_name="Garcia", stages=[])

    def _planning_status(self):
        self.item.refresh_from_db()
        return self.item.planning[self.STAGE]["status"]

    def _open_session_count(self):
        return ProductionItemSession.objects.filter(
            production_item=self.item,
            production_stage=self.STAGE,
            end_time__isnull=True,
        ).count()

    def test_finish_blocked_while_other_worker_active(self):
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.maria,
        )

        with self.assertRaises(StageHasActiveWorkersError) as ctx:
            StagesToSessionsService.finish_activity(
                stage_name=self.STAGE,
                items=[self.item],
                employee=self.jose,
            )

        self.assertEqual(ctx.exception.code, "stage_has_active_workers")
        self.assertEqual(len(ctx.exception.active_workers), 1)
        self.assertEqual(ctx.exception.active_workers[0]["id"], self.maria.id)
        self.assertEqual(self._planning_status(), ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(self._open_session_count(), 2)

    def test_leave_activity_closes_only_requester_session(self):
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.maria,
        )

        count = StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(self._open_session_count(), 1)

    def test_finish_succeeds_when_only_requester_active(self):
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.maria,
        )

        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        count = StagesToSessionsService.finish_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.maria,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)
        self.assertEqual(self._open_session_count(), 0)

        done_sessions = ProductionItemSession.objects.filter(
            production_item=self.item,
            production_stage=self.STAGE,
            status=ProductionItemSessionStatus.DONE,
        )
        self.assertEqual(done_sessions.count(), 2)

    def test_finish_succeeds_solo_worker(self):
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        count = StagesToSessionsService.finish_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)
        self.assertEqual(self._open_session_count(), 0)

    def test_finish_without_own_session_when_no_others_active(self):
        """Stage in progress with no open sessions (edge) can still be marked done."""
        self.item.planning = {
            self.STAGE: {
                "status": ProductionItemStatus.IN_PROGRESS,
                "started_at": timezone.now().isoformat(),
                "completed_at": None,
            },
        }
        self.item.save(update_fields=["planning"])

        count = StagesToSessionsService.finish_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)
