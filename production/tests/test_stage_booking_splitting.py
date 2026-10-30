from datetime import datetime
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from company.active_company import ActiveCompany
from production.models import ProductionItemSession, ProductionItemStatus
from production.services.production_planning import ProductionPlanningService
from production.services.stage_booking_splitting import StageBookingSplittingService as Splitting
from production.services.stages_to_sessions import StagesToSessionsService
from production.tests.factories import (
    STAGE_CUT, STAGE_WELD, add_schedule, make_company, make_employee, make_item,
    make_machine, make_session, make_staff_user, make_ticket, resource_for,
)


class StageBookingFixtures:
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)
        cls.employee = make_employee(cls.company)
        cls.operator = resource_for(cls.employee)
        cls.machine = resource_for(make_machine(cls.company))
        cls.ticket = make_ticket(cls.company, stages=[STAGE_CUT, STAGE_WELD],
                                 stage_minutes={STAGE_CUT: 120, STAGE_WELD: 60}, machine_qty=1)
        cls.item = make_item(cls.ticket, stages=[STAGE_CUT, STAGE_WELD])
        cls.other_company = make_company(name="Other company")
        cls.other_ticket = make_ticket(cls.other_company)
        cls.user = make_staff_user(username="split-planner", companies=[cls.company], perms=[
            "view_plannedproductionsession", "add_plannedproductionsession",
            "change_plannedproductionsession",
        ])

    def setUp(self):
        self.addCleanup(ActiveCompany.clear_current)

    def at(self, hour, minute=0, day=14):
        return timezone.make_aware(datetime(2026, 9, day, hour, minute))

    def booking(self, *, start=None, end=None, **kwargs):
        return make_session(self.ticket, start=start or self.at(9), end=end or self.at(11),
                            resources=[self.operator, self.machine], **kwargs)

    def split(self, booking, *, cut_at=None, **kwargs):
        return Splitting.split_booking(booking.pk, cut_at=cut_at or self.at(10),
                                      expected_start=booking.planned_start,
                                      expected_end=booking.planned_end, **kwargs)

    def move_day(self, booking, day=15, **kwargs):
        return ProductionPlanningService.move_session_day(
            booking.pk, day=self.at(9, day=day).date(), expected_start=booking.planned_start,
            expected_end=booking.planned_end, **kwargs,
        )


class StageBookingSplittingTest(StageBookingFixtures, TestCase):
    def test_cut_keeps_original_id_total_time_resources_notes_and_stage_sequence(self):
        original = self.booking(sequence=3, notes="Keep me")
        result = self.split(original)
        self.assertTrue(result.ok, result.conflicts)
        first, continuation = result.sessions
        self.assertEqual(first.pk, original.pk)
        self.assertEqual(first.planned_end, self.at(10))
        self.assertEqual(continuation.planned_start, self.at(10))
        self.assertEqual(continuation.planned_end, self.at(11))
        for booking in result.sessions:
            self.assertEqual(booking.notes, "Keep me")
            self.assertEqual(booking.sequence, 3)
            self.assertEqual(set(booking.resources.values_list("resource_id", flat=True)),
                             {self.machine.pk, self.operator.pk})
        self.assertEqual(sum(Splitting.working_minutes(s) for s in result.sessions), 120)
        self.item.refresh_from_db()
        self.assertEqual(self.item.planning[STAGE_CUT]["status"], ProductionItemStatus.PENDING)
        self.assertFalse(ProductionItemSession.objects.exists())

    def test_cut_at_lunch_starts_continuation_at_next_opening(self):
        original = self.booking(start=self.at(13, 30), end=self.at(16))
        result = self.split(original, cut_at=self.at(14))
        self.assertTrue(result.ok, result.conflicts)
        first, continuation = result.sessions
        self.assertEqual(first.planned_end, self.at(14))
        self.assertEqual(continuation.planned_start, self.at(15))
        self.assertEqual(continuation.planned_end, self.at(16))
        self.assertEqual([Splitting.working_minutes(s) for s in result.sessions], [30, 60])

    def test_cut_at_weekend_boundary_preserves_working_time(self):
        original = self.booking(start=self.at(17, day=18), end=self.at(9, day=21))
        result = self.split(original, cut_at=self.at(18, day=18))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(8, day=21))
        self.assertEqual([Splitting.working_minutes(s) for s in result.sessions], [60, 60])

    def test_edges_closed_time_and_subminute_parts_are_rejected(self):
        original = self.booking(start=self.at(13, 30), end=self.at(16))
        for cut_at in (self.at(13, 30), self.at(16), self.at(14, 30), self.at(13, 30).replace(second=30)):
            with self.subTest(cut_at=cut_at), self.assertRaises(ValidationError):
                self.split(original, cut_at=cut_at, override=True)
        original.refresh_from_db()
        self.assertEqual(original.planned_end, self.at(16))
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_underway_stage_uses_manual_cut_without_pausing_or_changing_actual_work(self):
        original = self.booking()
        with patch("django.utils.timezone.now", return_value=self.at(9)):
            StagesToSessionsService.start_activity(stage_name=STAGE_CUT, items=[self.item], employee=self.employee)
        with patch("django.utils.timezone.now", return_value=self.at(10, 30)):
            result = self.split(original, cut_at=self.at(10))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[0].planned_end, self.at(10))
        actual = ProductionItemSession.objects.get()
        self.assertEqual(actual.start_time, self.at(9))
        self.assertIsNone(actual.end_time)
        self.item.refresh_from_db()
        self.assertEqual(self.item.planning[STAGE_CUT]["status"], ProductionItemStatus.IN_PROGRESS)
        with patch("django.utils.timezone.now", return_value=self.at(11)):
            StagesToSessionsService.finish_activity(stage_name=STAGE_CUT, items=[self.item], employee=self.employee)
        self.item.refresh_from_db()
        self.assertEqual(self.item.planning[STAGE_CUT]["status"], ProductionItemStatus.DONE)
        self.assertEqual(self.ticket.planned_sessions.count(), 2)

    def test_repeat_with_stale_snapshot_cannot_create_duplicate_continuation(self):
        original = self.booking()
        self.assertTrue(self.split(original).ok)
        with self.assertRaises(ValidationError):
            self.split(original)
        self.assertEqual(self.ticket.planned_sessions.count(), 2)

    def test_a_continuation_can_be_split_again(self):
        original = self.booking()
        tail = self.split(original).sessions[1]
        self.assertTrue(self.split(tail, cut_at=self.at(10, 30)).ok)
        self.assertEqual(self.ticket.planned_sessions.count(), 3)
        self.assertEqual(sum(Splitting.working_minutes(s) for s in self.ticket.planned_sessions.all()), 120)

    def test_locked_or_finished_bookings_cannot_be_split(self):
        original = self.booking(locked=True)
        self.assertFalse(self.split(original, override=True).ok)
        original.locked = False
        original.save()
        self.item.planning[STAGE_CUT]["status"] = ProductionItemStatus.DONE
        self.item.save()
        with self.assertRaises(ValidationError):
            self.split(original)
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_warning_on_continuation_rolls_back_shortening_until_confirmed(self):
        original = self.booking()
        other = make_ticket(self.company)
        make_session(other, start=self.at(10), end=self.at(11), resources=[self.operator])
        result = self.split(original)
        self.assertFalse(result.ok)
        self.assertTrue(result.requires_confirmation)
        original.refresh_from_db()
        self.assertEqual(original.planned_end, self.at(11))
        self.assertEqual(self.ticket.planned_sessions.count(), 1)
        self.assertTrue(self.split(original, override=True).ok)
        self.assertEqual(self.ticket.planned_sessions.count(), 2)

    def test_moving_a_portion_to_another_day_preserves_working_minutes_and_resources(self):
        original = self.booking(start=self.at(13, 30), end=self.at(17))
        first, tail = self.split(original, cut_at=self.at(16)).sessions
        result = self.move_day(first)
        self.assertTrue(result.ok, result.conflicts)
        first.refresh_from_db()
        tail.refresh_from_db()
        self.assertEqual(first.planned_start, self.at(13, 30, day=15))
        self.assertEqual(first.planned_end, self.at(16, day=15))
        self.assertEqual(Splitting.working_minutes(first), 90)
        self.assertEqual(tail.planned_start, self.at(16))
        self.assertEqual(set(first.resources.values_list("resource_id", flat=True)),
                         {self.operator.pk, self.machine.pk})

    def test_week_move_warns_for_busy_resources_then_allows_confirmation(self):
        original = self.booking()
        other = make_ticket(self.company)
        make_session(other, start=self.at(9, day=15), end=self.at(11, day=15), resources=[self.operator])
        result = self.move_day(original)
        self.assertTrue(result.requires_confirmation)
        original.refresh_from_db()
        self.assertEqual(original.planned_start, self.at(9))
        self.assertTrue(self.move_day(original, override=True).ok)

    def test_week_move_cannot_overlap_another_part_even_when_confirmed(self):
        original = self.booking()
        first, tail = self.split(original).sessions
        self.assertTrue(ProductionPlanningService.move_session(tail.pk, new_start=self.at(9, day=15)).ok)
        result = self.move_day(first, override=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertIn("stage_part_overlap", [c.code for c in result.conflicts])

    def test_week_move_rejects_locked_stale_or_closed_day(self):
        original = self.booking()
        self.assertFalse(self.move_day(original, day=20).ok)
        self.assertTrue(self.move_day(original).ok)
        self.assertFalse(self.move_day(original, day=16).ok)
        original.refresh_from_db()
        original.locked = True
        original.save()
        self.assertFalse(self.move_day(original, day=16, override=True).ok)

    def test_ticket_reorder_keeps_parts_in_one_logical_stage(self):
        original = self.booking()
        self.split(original)
        weld = make_session(self.ticket, stage=STAGE_WELD, start=self.at(11), end=self.at(12),
                            resources=[self.operator, self.machine], sequence=1)
        result = ProductionPlanningService.reorder(self.ticket.pk, session_id=weld.pk,
                                                    before_session_id=original.pk, override=True)
        self.assertTrue(result.ok)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.stage_order, [STAGE_WELD, STAGE_CUT])
        self.assertEqual(list(self.ticket.planned_sessions.filter(production_stage=STAGE_CUT)
                              .values_list("sequence", flat=True)), [1, 1])
        result = ProductionPlanningService.reorder(self.ticket.pk, session_id=original.pk,
                                                    before_session_id=weld.pk, override=True)
        self.assertTrue(result.ok)
        original.refresh_from_db()
        self.assertEqual(original.planned_start, self.at(9))


class StageBookingSplittingViewTest(StageBookingFixtures, TestCase):
    def post_action(self, booking, action="split_booking", **changes):
        payload = {"action": action, "session_id": booking.pk, "cut_at": self.at(10).isoformat(),
                   "expected_start": booking.planned_start.isoformat(), "expected_end": booking.planned_end.isoformat(),
                   "day": "2026-09-15"}
        payload.update(changes)
        return self.client.post(reverse("admin:production_plannedproductionsession_schedule_action"),
                                payload, content_type="application/json")

    def test_split_requires_add_and_change_permissions(self):
        original = self.booking()
        for granted in ("add_plannedproductionsession", "change_plannedproductionsession"):
            user = make_staff_user(username=granted, companies=[self.company],
                                   perms=["view_plannedproductionsession", granted])
            self.client.force_login(user)
            self.assertEqual(self.post_action(original).status_code, 403)
        self.client.force_login(self.user)
        response = self.post_action(original)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_foreign_bookings_cannot_be_read_split_or_moved(self):
        self.client.force_login(self.user)
        foreign = make_session(self.other_ticket, start=self.at(9), end=self.at(11))
        url = reverse("admin:production_plannedproductionsession_schedule_action")
        self.assertEqual(self.client.get(url, {"session_id": foreign.pk}).status_code, 404)
        for action in ("split_booking", "move_session_day"):
            self.assertEqual(self.post_action(foreign, action).status_code, 404)

    def test_week_move_requires_change_permission(self):
        original = self.booking()
        user = make_staff_user(username="viewer", companies=[self.company], perms=["view_plannedproductionsession"])
        self.client.force_login(user)
        self.assertEqual(self.post_action(original, "move_session_day").status_code, 403)
        self.client.force_login(self.user)
        response = self.post_action(original, "move_session_day")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_cut_metadata_contains_working_windows_without_pause_configuration(self):
        self.client.force_login(self.user)
        original = self.booking(start=self.at(13, 30), end=self.at(16))
        response = self.client.get(reverse("admin:production_plannedproductionsession_schedule_action"),
                                   {"session_id": original.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["working_minutes"], 90)
        self.assertEqual([w["minutes"] for w in response.json()["working_windows"]], [30, 60])
        self.assertNotIn("can_pause", response.json())
        self.assertNotIn("active", response.json())

    def test_daily_and_weekly_calendars_show_scissors_without_configuration_modal(self):
        self.client.force_login(self.user)
        self.booking()
        for period in ("day", "week"):
            response = self.client.get(reverse("admin:production_plannedproductionsession_schedule"),
                                       {"day": "2026-09-14", "period": period})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "beginSplit($event)")
            self.assertContains(response, 'x-data="productionSchedule()"')
            self.assertNotContains(response, "ps-split-dialog")
            self.assertNotContains(response, "openStageParts")
            if period == "week":
                self.assertContains(response, 'data-drop-day="2026-09-15"')

    def test_old_pending_split_action_is_removed(self):
        self.client.force_login(self.user)
        original = self.booking()
        self.assertEqual(self.post_action(original, "plan_stage_parts").status_code, 400)
        self.assertEqual(self.ticket.planned_sessions.count(), 1)
