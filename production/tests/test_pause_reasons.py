import unicodedata
from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from company.models import Company, Employee
from configurator.models import Configuration, ProductTemplate
from production.models import (
    ProductionItem,
    ProductionItemStatus,
    ProductionTicket,
)
from production.services.stages_to_sessions import StagesToSessionsService


class PauseReasonsTest(TestCase):
    STAGE = "montaje"
    REASON = "Espera de piezas"

    def setUp(self):
        self.company = Company.objects.create(
            name="Test Co",
            allow_shared_production_stage_activities=True,
            production_stages=[self.STAGE],
            pause_reasons=[self.REASON, "Espera de cliente"],
        )
        self.product = ProductTemplate.objects.create(
            name="Ventana",
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

    def _stage_info(self):
        self.item.refresh_from_db()
        return self.item.planning[self.STAGE]

    def _start(self, employee):
        StagesToSessionsService.start_group_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=employee,
        )

    def test_leave_with_reason_records_it_on_planning(self):
        self._start(self.jose)

        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=self.REASON,
        )

        info = self._stage_info()
        self.assertEqual(info["status"], ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(info["pause_reason"], self.REASON)

    def test_leave_without_reason_keeps_planning_untouched(self):
        self._start(self.jose)

        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        self.assertNotIn("pause_reason", self._stage_info())

    def test_leave_with_unknown_reason_is_rejected(self):
        self._start(self.jose)

        with self.assertRaises(ValidationError):
            StagesToSessionsService.leave_activity(
                stage_name=self.STAGE,
                items=[self.item],
                employee=self.jose,
                pause_reason="Motivo inventado",
            )

        # Validation happens before anything is closed.
        self.assertNotIn("pause_reason", self._stage_info())

    def test_reason_not_recorded_while_other_worker_still_active(self):
        self._start(self.jose)
        self._start(self.maria)

        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=self.REASON,
        )

        # Maria keeps working: the stage is not paused, so no reason is stored.
        self.assertNotIn("pause_reason", self._stage_info())

    def test_reason_matching_is_nfc_tolerant(self):
        nfd_reason = unicodedata.normalize("NFD", self.REASON)
        self._start(self.jose)

        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=nfd_reason,
        )

        # The canonical company value is stored, not the raw request string.
        self.assertEqual(self._stage_info()["pause_reason"], self.REASON)

    def test_resume_clears_pause_reason(self):
        self._start(self.jose)
        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=self.REASON,
        )

        self._start(self.maria)

        self.assertNotIn("pause_reason", self._stage_info())

    def test_finish_clears_pause_reason(self):
        self.item.planning = {
            self.STAGE: {
                "status": ProductionItemStatus.IN_PROGRESS,
                "started_at": timezone.now().isoformat(),
                "completed_at": None,
                "pause_reason": self.REASON,
            },
        }
        self.item.save(update_fields=["planning"])

        StagesToSessionsService.finish_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
        )

        info = self._stage_info()
        self.assertEqual(info["status"], ProductionItemStatus.DONE)
        self.assertNotIn("pause_reason", info)

    def test_revert_clears_pause_reason(self):
        self.item.planning = {
            self.STAGE: {
                "status": ProductionItemStatus.IN_PROGRESS,
                "started_at": timezone.now().isoformat(),
                "completed_at": None,
                "pause_reason": self.REASON,
            },
        }
        self.item.save(update_fields=["planning"])

        StagesToSessionsService.revert_activity(
            stage_name=self.STAGE,
            items=[self.item],
            target_status=ProductionItemStatus.PENDING,
        )

        info = self._stage_info()
        self.assertEqual(info["status"], ProductionItemStatus.PENDING)
        self.assertNotIn("pause_reason", info)
