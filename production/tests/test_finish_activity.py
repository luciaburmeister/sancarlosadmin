from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from company.models import Company, Employee
from configurator.models import Configuration, ProductTemplate
from production.exceptions import StageHasActiveWorkersError
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
    ProductionTicket,
)
from production.services.stages_to_sessions import StagesToSessionsService


class FinishActivitySharedWorkersTest(TestCase):
    STAGE = "limpieza"

    def setUp(self):
        self.company = Company.objects.create(
            name="Test Co",
            allow_shared_production_stage_activities=True,
            production_stages=[self.STAGE],
        )
        self.product = ProductTemplate.objects.create(
            name="Wash product",
            company=self.company,
            production_stages=[self.STAGE],
        )
        self.config = Configuration.objects.create(
            company=self.company,
            product=self.product,
        )
        ct = ContentType.objects.get_for_model(self.config)
        self.ticket = ProductionTicket.objects.create(
            company=self.company,
            content_type=ct,
            object_id=self.config.id,
        )
        self.item = ProductionItem.objects.create(
            ticket=self.ticket,
            company=self.company,
            planning={
                self.STAGE: {
                    "status": ProductionItemStatus.PENDING,
                    "started_at": None,
                    "completed_at": None,
                },
            },
            production_stages_order=[self.STAGE],
        )
        hired = date(2020, 1, 1)
        self.jose = Employee.objects.create(
            company=self.company,
            first_name="Jose",
            last_name="Murillo",
            hired_at=hired,
            can_work_on_production=True,
        )
        self.maria = Employee.objects.create(
            company=self.company,
            first_name="Maria",
            last_name="Garcia",
            hired_at=hired,
            can_work_on_production=True,
        )

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
