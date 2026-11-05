import unicodedata

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from production.models import ProductionItemSession, ProductionItemStatus
from production.services.stages_to_sessions import StagesToSessionsService
from production.tests.factories import make_company, make_employee, make_item, make_product, make_ticket


class PauseReasonsTest(TestCase):
    STAGE = "montaje"
    REASON = "Espera de piezas"

    def setUp(self):
        self.company = make_company(
            stages=[self.STAGE],
            allow_shared_production_stage_activities=True,
            pause_reasons=[self.REASON, "Espera de cliente"],
        )
        product = make_product(self.company, name="Ventana", stages=[self.STAGE])
        self.ticket = make_ticket(self.company, stages=[self.STAGE], product=product, bom=False)
        self.item = make_item(self.ticket, stages=[self.STAGE])
        self.jose = make_employee(self.company, first_name="Jose", last_name="Murillo", stages=[])
        self.maria = make_employee(self.company, first_name="Maria", last_name="Garcia", stages=[])

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

    def test_release_paused_keeps_status_and_pause_reason(self):
        self._start(self.jose)
        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=self.REASON,
        )
        sessions_before = ProductionItemSession.objects.filter(production_item=self.item).count()

        StagesToSessionsService.release_paused_to_assigned(
            stage_name=self.STAGE,
            item=self.item,
        )

        info = self._stage_info()
        self.assertEqual(info["status"], ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(info["pause_reason"], self.REASON)
        self.assertTrue(info[StagesToSessionsService.RELEASED_TO_ASSIGNED])
        self.assertEqual(
            ProductionItemSession.objects.filter(production_item=self.item).count(),
            sessions_before,
        )
        self.assertFalse(
            ProductionItemSession.objects.filter(
                production_item=self.item, end_time__isnull=True
            ).exists()
        )

        self._start(self.jose)
        started = self._stage_info()
        self.assertEqual(started["status"], ProductionItemStatus.IN_PROGRESS)
        self.assertNotIn(StagesToSessionsService.RELEASED_TO_ASSIGNED, started)

    def test_update_pause_reason_changes_stored_value(self):
        self._start(self.jose)
        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=self.REASON,
        )
        sessions_before = ProductionItemSession.objects.filter(production_item=self.item).count()

        StagesToSessionsService.update_pause_reason(
            stage_name=self.STAGE,
            item=self.item,
            pause_reason="Espera de cliente",
        )

        info = self._stage_info()
        self.assertEqual(info["status"], ProductionItemStatus.IN_PROGRESS)
        self.assertEqual(info["pause_reason"], "Espera de cliente")
        self.assertEqual(
            ProductionItemSession.objects.filter(production_item=self.item).count(),
            sessions_before,
        )

    def test_update_pause_reason_rejected_while_someone_is_active(self):
        self._start(self.jose)

        with self.assertRaises(ValidationError):
            StagesToSessionsService.update_pause_reason(
                stage_name=self.STAGE,
                item=self.item,
                pause_reason="Espera de cliente",
            )

    def test_update_pause_reason_rejected_when_not_paused(self):
        with self.assertRaises(ValidationError):
            StagesToSessionsService.update_pause_reason(
                stage_name=self.STAGE,
                item=self.item,
                pause_reason=self.REASON,
            )

    def test_update_pause_reason_rejects_unknown_reason(self):
        self._start(self.jose)
        StagesToSessionsService.leave_activity(
            stage_name=self.STAGE,
            items=[self.item],
            employee=self.jose,
            pause_reason=self.REASON,
        )

        with self.assertRaises(ValidationError):
            StagesToSessionsService.update_pause_reason(
                stage_name=self.STAGE,
                item=self.item,
                pause_reason="Motivo inventado",
            )

        self.assertEqual(self._stage_info()["pause_reason"], self.REASON)

    def test_release_rejected_while_someone_is_active(self):
        self._start(self.jose)

        with self.assertRaises(ValidationError):
            StagesToSessionsService.release_paused_to_assigned(
                stage_name=self.STAGE,
                item=self.item,
            )

        self.assertEqual(self._stage_info()["status"], ProductionItemStatus.IN_PROGRESS)

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
