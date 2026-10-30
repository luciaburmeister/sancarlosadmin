from datetime import datetime, timedelta
from time import time
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from company.active_company import ActiveCompany
from configurator.models import ConfigurationStatus
from management.models import ProjectFile, ProjectFileStatus
from production.constants import ProductionActions
from production.models import ProductionItemSession, ProductionItemStatus, ProductionTicketStatus
from production.services.planning_backlog import unplanned_tickets
from production.services.stages_to_sessions import StagesToSessionsService as Stages
from production.tests.factories import (
    STAGE_CUT, STAGE_WELD, add_schedule, make_company, make_employee, make_item,
    make_session, make_staff_user, make_ticket,
)
from sales.models import Client


class CalendarStageCompletionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.ticket = make_ticket(cls.company, stages=[STAGE_CUT, STAGE_WELD])
        cls.items = [make_item(cls.ticket, stages=[STAGE_CUT, STAGE_WELD]) for _ in range(2)]
        cls.user = make_staff_user(username="calendar-supervisor", companies=[cls.company],
                                   perms=["view_plannedproductionsession", "change_productionitem"])
        cls.employee = make_employee(cls.company, user=cls.user, is_production_supervisor=True)
        cls.worker_user = make_staff_user(username="calendar-worker", companies=[cls.company],
                                          perms=["view_plannedproductionsession", "change_productionitem"])
        cls.worker = make_employee(cls.company, user=cls.worker_user)
        cls.other_company = make_company(name="Other company")
        cls.other_ticket = make_ticket(cls.other_company, stages=[STAGE_CUT])
        cls.other_item = make_item(cls.other_ticket, stages=[STAGE_CUT])

    def setUp(self):
        self.addCleanup(ActiveCompany.clear_current)
        self.client.force_login(self.user)

    def post(self, stage=STAGE_CUT, **kwargs):
        payload = {"action": "complete_stage", "ticket_id": self.ticket.pk, "stage": stage}
        payload.update(kwargs)
        return self.client.post(reverse("admin:production_plannedproductionsession_schedule_action"),
                                payload, content_type="application/json")

    def assert_stage_pending(self):
        for item in self.items:
            item.refresh_from_db()
            self.assertEqual(item.planning[STAGE_CUT]["status"], ProductionItemStatus.PENDING)

    def test_finishes_without_bookings_or_work_records_and_last_stage_hides_ticket(self):
        self.assertFalse(self.items[0].can_finish_pending())
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["completed_items"], 2)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, ProductionTicketStatus.IN_PROGRESS)
        for item in self.items:
            item.refresh_from_db()
            self.assertEqual(item.planning[STAGE_CUT]["status"], ProductionItemStatus.DONE)
            self.assertEqual(item.planning[STAGE_CUT]["completed_by"], self.employee.pk)
            self.assertEqual(item.planning[STAGE_CUT]["completed_by_user"], self.user.pk)
            self.assertTrue(item.planning[STAGE_CUT]["completed_at"])
        self.assertEqual([stage["stage"] for stage in unplanned_tickets(self.company)[0].stages], [STAGE_WELD])
        self.assertTrue(self.post(STAGE_WELD).json()["ok"])
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, ProductionTicketStatus.DONE)
        self.assertEqual(unplanned_tickets(self.company), [])
        self.assertEqual(self.ticket.source_object.status, ConfigurationStatus.PRODUCTION_DONE)
        self.assertFalse(self.ticket.planned_sessions.exists())
        self.assertFalse(ProductionItemSession.objects.exists())

    def test_repeat_keeps_original_completion_audit(self):
        self.assertTrue(self.post().json()["ok"])
        self.items[0].refresh_from_db()
        original = self.items[0].planning[STAGE_CUT].copy()
        response = self.post()
        self.assertEqual(response.json()["completed_items"], 0)
        self.items[0].refresh_from_db()
        self.assertEqual(self.items[0].planning[STAGE_CUT], original)

    def test_completing_more_items_does_not_repeat_ticket_status_queries(self):
        with CaptureQueriesContext(connection) as small:
            self.assertTrue(self.post().json()["ok"])
        for _ in range(30):
            make_item(self.ticket, stages=[STAGE_CUT, STAGE_WELD])
        with CaptureQueriesContext(connection) as large:
            response = self.post(STAGE_WELD)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["completed_items"], 32)
        self.assertLessEqual(len(large), len(small) + 3)

    def test_already_finished_items_are_preserved_while_other_items_finish(self):
        Stages.finish_activity(stage_name=STAGE_CUT, items=[self.items[0]], employee=self.worker, allow_pending=True)
        self.assertEqual(self.post().json()["completed_items"], 1)
        self.items[0].refresh_from_db()
        self.items[1].refresh_from_db()
        self.assertEqual(self.items[0].planning[STAGE_CUT]["completed_by"], self.worker.pk)
        self.assertEqual(self.items[1].planning[STAGE_CUT]["completed_by"], self.employee.pk)

    def test_active_worker_or_supervisor_blocks_all_items_without_closing_work(self):
        for employee in (self.worker, self.employee):
            with self.subTest(employee=employee.pk):
                Stages.start_activity(stage_name=STAGE_CUT, items=[self.items[1]], employee=employee)
                response = self.post()
                self.assertEqual(response.status_code, 400)
                self.items[0].refresh_from_db()
                self.assertEqual(self.items[0].planning[STAGE_CUT]["status"], ProductionItemStatus.PENDING)
                session = ProductionItemSession.objects.get(end_time__isnull=True)
                self.assertEqual(session.created_by_id, employee.pk)
                Stages.leave_activity(stage_name=STAGE_CUT, items=[self.items[1]], employee=employee)

    def test_paused_work_keeps_its_recorded_time_without_adding_a_session(self):
        start = timezone.make_aware(datetime(2026, 10, 27, 9))
        with patch("django.utils.timezone.now", return_value=start):
            Stages.start_activity(stage_name=STAGE_CUT, items=[self.items[0]], employee=self.worker)
        with patch("django.utils.timezone.now", return_value=start + timedelta(minutes=30)):
            Stages.leave_activity(stage_name=STAGE_CUT, items=[self.items[0]], employee=self.worker)
        session = ProductionItemSession.objects.get()
        self.assertTrue(self.post().json()["ok"])
        session.refresh_from_db()
        self.assertEqual(ProductionItemSession.objects.count(), 1)
        self.assertEqual(session.end_time, start + timedelta(minutes=30))

    def test_reopening_restores_the_unplanned_stage_to_pending(self):
        self.post()
        self.post(STAGE_WELD)
        for item in self.items:
            item.refresh_from_db()
        Stages.revert_activity(stage_name=STAGE_WELD, items=self.items,
                               target_status=ProductionItemStatus.PENDING, user=self.user, employee=self.employee)
        restored = unplanned_tickets(self.company)[0]
        self.assertEqual(restored.ticket_id, self.ticket.pk)
        self.assertEqual([stage["stage"] for stage in restored.stages], [STAGE_WELD])
        self.assertFalse(restored.stages[0]["is_planned"])

    def test_existing_calendar_parts_remain_as_with_normal_completion(self):
        start = timezone.make_aware(datetime(2026, 10, 27, 9))
        for offset in (0, 2):
            make_session(self.ticket, start=start + timedelta(hours=offset),
                         end=start + timedelta(hours=offset + 1))
        self.assertTrue(self.post().json()["ok"])
        self.assertEqual(self.ticket.planned_sessions.count(), 2)
        self.assertFalse(ProductionItemSession.objects.exists())

    def test_invalid_stage_and_cancelled_stage_cannot_partially_finish_items(self):
        for stage in ("missing", "", None, [STAGE_CUT]):
            self.assertEqual(self.post(stage).status_code, 400)
        self.items[1].planning[STAGE_CUT]["status"] = ProductionItemStatus.CANCELLED
        self.items[1].save()
        self.assertEqual(self.post().status_code, 400)
        self.items[0].refresh_from_db()
        self.assertEqual(self.items[0].planning[STAGE_CUT]["status"], ProductionItemStatus.PENDING)

    def test_cancelled_ticket_is_not_finished(self):
        self.ticket.status = ProductionTicketStatus.CANCELLED
        self.ticket.save()
        self.assertEqual(self.post().status_code, 400)
        self.assert_stage_pending()

    def test_ticket_in_cancelled_project_file_is_not_finished(self):
        client = Client.objects.create(company=self.company, name="Completion test client")
        self.ticket.project_file = ProjectFile.objects.create(
            company=self.company, client=client, name="Cancelled project", status=ProjectFileStatus.CANCELLED,
        )
        self.ticket.save()
        self.assertEqual(self.post().status_code, 400)
        self.assert_stage_pending()

    def test_other_units_still_hold_configuration_completion_back(self):
        sibling = make_ticket(self.company, configuration=self.ticket.source_object,
                              stages=[STAGE_CUT, STAGE_WELD], bom=False, unit_index=2)
        make_item(sibling, stages=[STAGE_CUT, STAGE_WELD])
        self.assertTrue(self.post().json()["ok"])
        self.assertTrue(self.post(STAGE_WELD).json()["ok"])
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, ProductionTicketStatus.DONE)
        self.assertNotEqual(self.ticket.source_object.status, ConfigurationStatus.PRODUCTION_DONE)

    def test_inconsistent_item_company_is_rejected(self):
        type(self.items[1]).objects.filter(pk=self.items[1].pk).update(company=self.other_company)
        self.assertEqual(self.post().status_code, 400)
        self.assert_stage_pending()

    def test_supervisor_of_another_company_cannot_complete_stage(self):
        self.employee.company = self.other_company
        self.employee.save()
        self.assertEqual(self.post().status_code, 403)
        self.assert_stage_pending()

    def test_completion_rolls_back_if_not_every_item_can_be_finished(self):
        original = Stages.finish_activity

        def finish_one(**kwargs):
            return original(**{**kwargs, "items": kwargs["items"][:1]})

        with patch.object(Stages, "finish_activity", side_effect=finish_one):
            self.assertEqual(self.post().status_code, 400)
        self.assert_stage_pending()

    def test_workers_and_office_staff_cannot_bypass_supervisor_check(self):
        self.client.force_login(self.worker_user)
        self.assertEqual(self.post().status_code, 403)
        self.worker.can_work_on_production = False
        self.worker.save()
        self.assertEqual(self.post().status_code, 403)
        self.assert_stage_pending()

    def test_supervisor_also_requires_item_change_permission(self):
        self.user.user_permissions.clear()
        self.assertEqual(self.post().status_code, 403)
        self.assert_stage_pending()

    def test_foreign_ticket_cannot_be_completed(self):
        self.assertEqual(self.post(ticket_id=self.other_ticket.pk).status_code, 404)
        self.other_item.refresh_from_db()
        self.assertEqual(self.other_item.planning[STAGE_CUT]["status"], ProductionItemStatus.PENDING)

    def test_foreign_or_inactive_employee_cannot_complete_stage(self):
        for changes in ({"company": self.other_company}, {"is_active": False}):
            with self.subTest(changes=changes):
                employee = make_employee(self.other_company, first_name="Other")
                if "is_active" in changes:
                    employee.company = self.company
                    employee.is_active = False
                    employee.save()
                with self.assertRaises(ValidationError):
                    Stages.complete_ticket_stage(ticket=self.ticket, stage_name=STAGE_CUT, employee=employee)
        self.assert_stage_pending()

    def login_without_employee(self):
        user = make_staff_user(username="calendar-no-employee", companies=[self.company])
        user.is_superuser = True
        user.save()
        self.client.force_login(user)
        # Superuser company ids are cached across tests; stale ids hide self.company.
        cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
        session = self.client.session
        session[ActiveCompany.SESSION_KEY] = [self.company.pk]
        session[ActiveCompany.SESSION_SET_AT_KEY] = time()
        session.save()
        return user

    def test_superuser_without_employee_can_complete_with_user_audit_and_no_work_time(self):
        user = self.login_without_employee()
        self.assertTrue(self.post().json()["ok"])
        self.assertTrue(self.post(STAGE_WELD).json()["ok"])
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, ProductionTicketStatus.DONE)
        self.assertEqual(unplanned_tickets(self.company), [])
        self.assertFalse(ProductionItemSession.objects.exists())
        self.assertFalse(self.ticket.planned_sessions.exists())
        for item in self.items:
            item.refresh_from_db()
            info = item.planning[STAGE_WELD]
            self.assertEqual(info["completed_by_user"], user.pk)
            self.assertNotIn("completed_by", info)
            self.assertEqual(info["completion_log"][0]["user_id"], user.pk)
            self.assertEqual(info["completion_log"][0]["user"], user.get_username())
            self.assertEqual(info["completion_log"][0]["at"], info["completed_at"])

    def test_no_employee_completion_can_be_reopened_and_preserves_its_audit(self):
        user = self.login_without_employee()
        self.assertTrue(self.post(STAGE_WELD).json()["ok"])
        self.assertEqual(self.post(STAGE_WELD).json()["completed_items"], 0)
        for item in self.items:
            item.refresh_from_db()
        response = self.client.post(
            reverse("admin:production_productionticket_stage_action", args=[self.ticket.pk, STAGE_WELD]),
            {"action": ProductionActions.REOPEN_STAGE, "target": ProductionItemStatus.PENDING,
             "item_ids": ",".join(str(item.pk) for item in self.items)},
        )
        self.assertEqual(response.status_code, 302)
        for item in self.items:
            item.refresh_from_db()
            info = item.planning[STAGE_WELD]
            self.assertEqual(info["status"], ProductionItemStatus.PENDING)
            self.assertIsNone(info["completed_at"])
            self.assertNotIn("completed_by_user", info)
            self.assertEqual(len(info["completion_log"]), 1)
            self.assertEqual(info["completion_log"][0]["user_id"], user.pk)
        self.assertTrue(self.post(STAGE_WELD).json()["ok"])
        self.items[0].refresh_from_db()
        self.assertEqual(len(self.items[0].planning[STAGE_WELD]["completion_log"]), 2)

    def test_no_employee_completion_still_rejects_active_work_and_foreign_tickets(self):
        Stages.start_activity(stage_name=STAGE_CUT, items=[self.items[0]], employee=self.worker)
        self.login_without_employee()
        self.assertEqual(self.post().status_code, 400)
        self.assertIsNone(ProductionItemSession.objects.get().end_time)
        self.assertEqual(self.post(ticket_id=self.other_ticket.pk).status_code, 404)

    def test_office_account_without_employee_does_not_gain_supervisor_access(self):
        user = make_staff_user(username="calendar-office-no-employee", companies=[self.company],
                               perms=["view_plannedproductionsession", "change_productionitem"])
        self.client.force_login(user)
        self.assertEqual(self.post().status_code, 403)
        self.assert_stage_pending()

    def test_supervisor_can_see_stage_button_without_calendar_add_permission(self):
        url = reverse("admin:production_plannedproductionsession_schedule")
        response = self.client.get(url, {"day": "2026-10-27"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-can-plan="0"')
        self.assertContains(response, 'data-can-complete-stage="1"')
        self.assertContains(response, "confirmCompleteStage(")
        self.assertContains(response, "toggleTicketStages(")
        self.client.force_login(self.worker_user)
        response = self.client.get(url, {"day": "2026-10-27"})
        self.assertContains(response, 'data-can-complete-stage="0"')
        self.assertNotContains(response, "confirmCompleteStage(")
