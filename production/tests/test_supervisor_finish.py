from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from company.models import Company, Employee
from configurator.models import Configuration, ProductTemplate
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
    ProductionTicket,
)
from production.services.stages_to_sessions import StagesToSessionsService


class SupervisorFinishActivityTest(TestCase):
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

    def test_close_from_pending_creates_retroactive_session(self):
        count = StagesToSessionsService.supervisor_finish_activity(
            stage_name=self.STAGE,
            item=self.item,
            attributions=[(self.jose, 3600)],
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)

        session = ProductionItemSession.objects.get(
            production_item=self.item,
            production_stage=self.STAGE,
            created_by=self.jose,
        )
        self.assertEqual(session.status, ProductionItemSessionStatus.DONE)
        self.assertIsNotNone(session.start_time)
        self.assertIsNotNone(session.end_time)
        self.assertEqual(
            int((session.end_time - session.start_time).total_seconds()),
            3600,
        )

    def test_close_active_worker_with_explicit_duration(self):
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        count = StagesToSessionsService.supervisor_finish_activity(
            stage_name=self.STAGE,
            item=self.item,
            attributions=[(self.jose, 1800)],
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)

        session = ProductionItemSession.objects.get(
            production_item=self.item,
            production_stage=self.STAGE,
            created_by=self.jose,
        )
        self.assertEqual(
            int((session.end_time - session.start_time).total_seconds()),
            1800,
        )

    def test_close_with_two_employees(self):
        count = StagesToSessionsService.supervisor_finish_activity(
            stage_name=self.STAGE,
            item=self.item,
            attributions=[(self.jose, 3600), (self.maria, 1200)],
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)
        self.assertEqual(
            ProductionItemSession.objects.filter(
                production_item=self.item,
                production_stage=self.STAGE,
                status=ProductionItemSessionStatus.DONE,
            ).count(),
            2,
        )

    def test_active_worker_not_in_attributions_is_paused_then_stage_closed(self):
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

        count = StagesToSessionsService.supervisor_finish_activity(
            stage_name=self.STAGE,
            item=self.item,
            attributions=[(self.maria, 2400)],
        )

        self.assertEqual(count, 1)
        self.assertEqual(self._planning_status(), ProductionItemStatus.DONE)

        jose_session = ProductionItemSession.objects.get(
            production_item=self.item,
            production_stage=self.STAGE,
            created_by=self.jose,
        )
        self.assertEqual(jose_session.status, ProductionItemSessionStatus.DONE)
        self.assertIsNotNone(jose_session.end_time)

        maria_session = ProductionItemSession.objects.get(
            production_item=self.item,
            production_stage=self.STAGE,
            created_by=self.maria,
        )
        self.assertEqual(
            int((maria_session.end_time - maria_session.start_time).total_seconds()),
            2400,
        )

    def test_rejects_already_done_stage(self):
        self.item.planning = {
            self.STAGE: {
                "status": ProductionItemStatus.DONE,
                "started_at": timezone.now().isoformat(),
                "completed_at": timezone.now().isoformat(),
            },
        }
        self.item.save(update_fields=["planning"])

        with self.assertRaises(ValidationError):
            StagesToSessionsService.supervisor_finish_activity(
                stage_name=self.STAGE,
                item=self.item,
                attributions=[(self.jose, 3600)],
            )

    def test_rejects_empty_attributions(self):
        with self.assertRaises(ValidationError):
            StagesToSessionsService.supervisor_finish_activity(
                stage_name=self.STAGE,
                item=self.item,
                attributions=[],
            )

    def test_rejects_duplicate_employee(self):
        with self.assertRaises(ValidationError):
            StagesToSessionsService.supervisor_finish_activity(
                stage_name=self.STAGE,
                item=self.item,
                attributions=[(self.jose, 3600), (self.jose, 1200)],
            )

    def test_rejects_too_short_duration(self):
        with self.assertRaises(ValidationError):
            StagesToSessionsService.supervisor_finish_activity(
                stage_name=self.STAGE,
                item=self.item,
                attributions=[(self.jose, 30)],
            )
