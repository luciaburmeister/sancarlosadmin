from datetime import time, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from production.models import ProductionItemSession
from production.services.production_planning import ProductionPlanningService as Planning
from production.services.stage_booking_splitting import StageBookingSplittingService as Splitting
from production.services.stages_to_sessions import StagesToSessionsService
from production.tests.factories import (
    STAGE_CUT, make_machine, make_session, make_staff_user, make_ticket, resource_for,
)
from production.tests.test_stage_booking_splitting import StageBookingFixtures


class TimetableOverflowTest(StageBookingFixtures, TestCase):
    def move(self, booking, *, start=None, plan=None, **kwargs):
        return Planning.move_session(
            booking.pk, new_start=start or self.at(17),
            timetable={"overflow_plan": plan}, **kwargs,
        )

    def confirm(self, booking, offer, **kwargs):
        return self.move(booking, plan=offer.overflow_plan, override=True, **kwargs)

    def test_overflow_is_only_an_offer_until_confirmed_and_preserves_resources_and_minutes(self):
        booking = self.booking(notes="Keep notes")
        offer = self.move(booking)
        self.assertFalse(offer.ok)
        self.assertTrue(offer.requires_confirmation)
        booking.refresh_from_db()
        self.assertEqual((booking.planned_start, booking.planned_end), (self.at(9), self.at(11)))
        self.assertEqual(self.ticket.planned_sessions.count(), 1)
        result = self.confirm(booking, offer)
        self.assertTrue(result.ok, result.conflicts)
        first, second = result.sessions
        self.assertEqual(first.pk, booking.pk)
        self.assertEqual((first.planned_start, first.planned_end), (self.at(17), self.at(18)))
        self.assertEqual((second.planned_start, second.planned_end), (self.at(8, day=15), self.at(9, day=15)))
        self.assertEqual(sum(Splitting.working_minutes(s) for s in result.sessions), 120)
        for part in result.sessions:
            self.assertEqual(part.notes, "Keep notes")
            self.assertEqual(part.sequence, booking.sequence)
            self.assertEqual(set(part.resources.values_list("resource_id", flat=True)),
                             {self.operator.pk, self.machine.pk})

    def test_remainder_waits_until_both_operator_and_machine_are_available(self):
        other = make_ticket(self.company)
        make_session(other, start=self.at(8, day=15), end=self.at(10, day=15), resources=[self.operator])
        make_session(other, start=self.at(10, day=15), end=self.at(11, day=15), resources=[self.machine])
        booking = self.booking()
        result = self.confirm(booking, self.move(booking))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(11, day=15))

    def test_remainder_respects_a_legacy_machine_anchor_without_m2m_assignments(self):
        busy = make_session(make_ticket(self.company), start=self.at(8, day=15),
                            end=self.at(10, day=15), resources=[self.machine])
        busy.resources.all().delete()
        booking = self.booking()
        result = self.confirm(booking, self.move(booking))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(10, day=15))

    def test_unattended_machine_parallel_capacity_is_respected(self):
        oven = resource_for(make_machine(self.company, name="Oven", needs_human=False, parallel_capacity=2))
        other = make_ticket(self.company)
        make_session(other, start=self.at(8, day=15), end=self.at(10, day=15), resources=[oven])
        booking = make_session(self.ticket, start=self.at(9), end=self.at(11), resources=[oven])
        result = self.confirm(booking, self.move(booking))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(8, day=15))

    def test_full_unattended_machine_on_the_selected_day_still_blocks(self):
        oven = resource_for(make_machine(self.company, name="Full oven", needs_human=False, parallel_capacity=1))
        make_session(make_ticket(self.company), start=self.at(17), end=self.at(18), resources=[oven])
        booking = make_session(self.ticket, start=self.at(9), end=self.at(11), resources=[oven])
        result = self.move(booking, override=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.blocked)
        self.assertIsNone(result.overflow_plan)
        booking.refresh_from_db()
        self.assertEqual(booking.planned_start, self.at(9))

    def test_daily_capacity_can_make_the_next_day_unavailable_without_overlaps(self):
        machine = self.machine.target
        machine.daily_capacity_minutes = 120
        machine.save()
        make_session(make_ticket(self.company), start=self.at(8, day=15), end=self.at(10, day=15),
                     resources=[self.machine])
        booking = self.booking()
        result = self.confirm(booking, self.move(booking))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(8, day=16))

    def test_lunch_does_not_count_and_long_remainder_is_split_at_daily_closing(self):
        booking = self.booking(start=self.at(8), end=self.at(18))
        offer = self.move(booking, start=self.at(17), duration_minutes=1200)
        result = self.confirm(booking, offer, start=self.at(17), duration_minutes=1200)
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual([Splitting.working_minutes(s) for s in result.sessions], [60, 540, 540, 60])
        self.assertTrue(all(s.planned_end.hour != 19 for s in result.sessions))

    def test_lunch_gap_within_the_same_day_does_not_trigger_daily_split(self):
        booking = self.booking()
        result = self.move(booking, start=self.at(13))
        self.assertTrue(result.ok, result.conflicts)
        self.assertIsNone(result.overflow_plan)
        self.assertEqual(result.sessions[0].planned_end, self.at(16))
        self.assertEqual(Splitting.working_minutes(result.sessions[0]), 120)
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_weekend_and_drop_after_closing_skip_to_next_working_day(self):
        booking = self.booking()
        offer = self.move(booking, start=self.at(18, day=18))
        self.assertEqual(len(offer.overflow_plan["parts"]), 1)
        result = self.confirm(booking, offer, start=self.at(18, day=18))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[0].pk, booking.pk)
        self.assertEqual(result.sessions[0].planned_start, self.at(8, day=21))
        self.assertEqual(result.sessions[0].planned_end, self.at(10, day=21))

    def test_existing_stage_portion_still_cannot_overlap(self):
        booking = self.booking()
        make_session(self.ticket, start=self.at(8, day=15), end=self.at(10, day=15),
                     resources=[], sequence=booking.sequence)
        result = self.confirm(booking, self.move(booking))
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(10, day=15))

    def test_changed_availability_requires_confirmation_of_a_new_proposal(self):
        booking = self.booking()
        offer = self.move(booking)
        other = make_ticket(self.company)
        make_session(other, start=self.at(8, day=15), end=self.at(10, day=15), resources=[self.operator])
        changed = self.confirm(booking, offer)
        self.assertFalse(changed.ok)
        self.assertTrue(changed.requires_confirmation)
        self.assertNotEqual(changed.overflow_plan, offer.overflow_plan)
        booking.refresh_from_db()
        self.assertEqual(booking.planned_start, self.at(9))
        result = self.confirm(booking, changed)
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(10, day=15))

    def test_stale_source_or_repeated_confirmation_does_not_move_or_duplicate_work(self):
        booking = self.booking()
        offer = self.move(booking)
        self.assertTrue(self.confirm(booking, offer).ok)
        repeated = self.confirm(booking, offer)
        self.assertFalse(repeated.ok)
        self.assertEqual(repeated.conflicts[0].code, "stale_booking")
        self.assertEqual(self.ticket.planned_sessions.count(), 2)

    def test_changed_working_hours_cannot_silently_replace_the_accepted_times(self):
        booking = self.booking()
        offer = self.move(booking)
        self.company.schedule_slots.filter(end_time=time(18)).update(end_time=time(20))
        result = self.confirm(booking, offer)
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts[0].code, "calendar_changed")
        booking.refresh_from_db()
        self.assertEqual(booking.planned_start, self.at(9))
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_warning_override_alone_cannot_accept_an_overflow_plan(self):
        booking = self.booking()
        offer = self.move(booking, override=True)
        self.assertTrue(offer.requires_confirmation)
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_locked_or_finished_bookings_are_not_split(self):
        booking = self.booking(locked=True)
        self.assertFalse(self.move(booking, override=True).ok)
        booking.locked = False
        booking.save()
        self.item.planning[STAGE_CUT]["status"] = "done"
        self.item.save()
        self.assertFalse(self.move(booking, override=True).ok)
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_no_slot_within_search_limit_does_not_change_any_booking(self):
        booking = self.booking()
        other = make_ticket(self.company)
        make_session(other, start=self.at(8, day=15), end=self.at(8, day=15) + timedelta(days=61),
                     resources=[self.operator])
        with CaptureQueriesContext(connection) as queries:
            result = self.move(booking, override=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts[0].code, "no_available_slot")
        self.assertLessEqual(len(queries), 30)
        booking.refresh_from_db()
        self.assertEqual(booking.planned_start, self.at(9))

    def test_week_move_also_offers_overflow_with_working_duration(self):
        booking = self.booking(start=self.at(17), end=self.at(9, day=15))
        options = dict(day=self.at(9, day=16).date(), expected_start=booking.planned_start,
                       expected_end=booking.planned_end)
        offer = Planning.move_session_day(booking.pk, timetable={}, **options)
        self.assertTrue(offer.requires_confirmation)
        result = Planning.move_session_day(booking.pk, timetable={"overflow_plan": offer.overflow_plan},
                                          override=True, **options)
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(result.sessions[1].planned_start, self.at(8, day=17))

    def test_pending_operator_drop_keeps_auto_assigned_machine_on_both_parts(self):
        stage = {"stage": STAGE_CUT, "duration_minutes": 120, "resource_ids": [self.operator.pk]}
        offer = Planning.plan_ticket_stage_custom(self.ticket.pk, start_at=self.at(17), stage=stage,
                                                  assign_machines=True, timetable={})
        self.assertTrue(offer.requires_confirmation)
        self.assertFalse(self.ticket.planned_sessions.exists())
        result = Planning.plan_ticket_stage_custom(
            self.ticket.pk, start_at=self.at(17), stage=stage, assign_machines=True,
            timetable={"overflow_plan": offer.overflow_plan}, override=True,
        )
        self.assertTrue(result.ok, result.conflicts)
        self.assertEqual(len(result.sessions), 2)
        for part in result.sessions:
            self.assertEqual(set(part.resources.values_list("resource_id", flat=True)),
                             {self.machine.pk, self.operator.pk})

    def test_underway_split_does_not_pause_actual_work(self):
        booking = self.booking()
        with patch("django.utils.timezone.now", return_value=self.at(9)):
            StagesToSessionsService.start_activity(stage_name=STAGE_CUT, items=[self.item], employee=self.employee)
        result = self.confirm(booking, self.move(booking))
        self.assertTrue(result.ok, result.conflicts)
        self.assertIsNone(ProductionItemSession.objects.get().end_time)

    def test_non_timetable_callers_keep_existing_warning_contract(self):
        booking = self.booking()
        result = Planning.move_session(booking.pk, new_start=self.at(17))
        self.assertTrue(result.requires_confirmation)
        self.assertIsNone(result.overflow_plan)
        result = Planning.move_session(booking.pk, new_start=self.at(17), override=True)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(result.sessions[0].planned_end, self.at(19))


class TimetableOverflowViewTest(StageBookingFixtures, TestCase):
    def post(self, booking, **kwargs):
        payload = {"action": "move_session", "session_id": booking.pk, "start": self.at(17).isoformat()}
        payload.update(kwargs)
        return self.client.post(reverse("admin:production_plannedproductionsession_schedule_action"),
                                payload, content_type="application/json")

    def test_calendar_offer_can_be_confirmed(self):
        self.client.force_login(self.user)
        booking = self.booking()
        response = self.post(booking)
        self.assertEqual(response.status_code, 200)
        offer = response.json()
        self.assertTrue(offer["requires_confirmation"])
        self.assertEqual([p["minutes"] for p in offer["overflow_plan"]["parts"]], [60, 60])
        response = self.post(booking, overflow_plan=offer["overflow_plan"], override=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(len(response.json()["sessions"]), 2)

    def test_confirmation_requires_add_permission_and_cannot_target_another_company(self):
        booking = self.booking()
        user = make_staff_user(username="overflow-no-add", companies=[self.company],
                               perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        self.assertEqual(self.post(booking, overflow_plan={}, override=True).status_code, 403)
        self.client.force_login(self.user)
        foreign = make_session(self.other_ticket, start=self.at(9), end=self.at(11))
        self.assertEqual(self.post(foreign).status_code, 404)
        foreign_resource = resource_for(make_machine(self.other_company, name="Foreign machine"))
        self.assertEqual(self.post(booking, resource_ids=[foreign_resource.pk]).status_code, 409)
        self.assertEqual(self.ticket.planned_sessions.count(), 1)

    def test_change_only_planners_keep_the_existing_move_warning(self):
        user = make_staff_user(username="overflow-move-only", companies=[self.company],
                               perms=["view_plannedproductionsession", "change_plannedproductionsession"])
        self.client.force_login(user)
        booking = self.booking()
        offer = self.post(booking).json()
        self.assertTrue(offer["requires_confirmation"])
        self.assertIsNone(offer["overflow_plan"])
        result = self.post(booking, override=True).json()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sessions"]), 1)
