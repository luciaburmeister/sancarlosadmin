from datetime import time, timedelta
from unittest.mock import patch
from django.urls import reverse

from api.production.tests.test_schedule_board_view import ScheduleBoardApiTestBase
from api.production.views.terminal import _build_planned_index
from production.models import PlannedProductionSession
from production.services.session_resources import sync_session_resources
from production.services.stage_booking_splitting import StageBookingSplittingService
from production.tests.factories import make_machine, make_session, make_ticket, resource_for


class SplitBookingApiCompatibilityTest(ScheduleBoardApiTestBase):
    def test_rejected_split_reorder_rolls_back_every_temporary_position(self):
        bookings, _ = self.split_bookings()
        machine = resource_for(make_machine(self.company, name="Second cutter"))
        sync_session_resources(bookings[1], [machine.pk, self.resource.pk])
        make_session(make_ticket(self.company), resources=[machine],
                     start=self._aware(self.MONDAY, time(9)), end=self._aware(self.MONDAY, time(10)))
        before = {s.pk: (s.planned_start, s.planned_end) for s in bookings}
        response = self.client.post(reverse("schedule_sessions_reorder_day"), {
            "supervisor_id": self.supervisor.pk, "employee_id": self.worker.pk,
            "date": self.MONDAY.isoformat(), "session_ids": [bookings[1].pk, bookings[0].pk],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        for booking in bookings:
            booking.refresh_from_db()
            self.assertEqual((booking.planned_start, booking.planned_end), before[booking.pk])

    def test_foreign_booking_with_an_invalid_local_anchor_cannot_be_moved(self):
        bookings, _ = self.split_bookings()
        PlannedProductionSession.objects.filter(pk=bookings[1].pk).update(company=self.other_company)
        response = self.client.post(reverse("schedule_session_reschedule", args=[bookings[1].pk]), {
            "supervisor_id": self.supervisor.pk, "date": self.MONDAY.isoformat(), "start_time": "12:00",
        }, format="json")
        self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse("schedule_session_delete", args=[bookings[1].pk]), {
            "supervisor_id": self.supervisor.pk,
        }, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PlannedProductionSession.objects.filter(pk=bookings[1].pk).exists())

    def test_date_only_move_finds_a_slot_free_for_both_machine_and_operator(self):
        bookings, machine = self.split_bookings()
        make_session(make_ticket(self.company), resources=[machine],
                     start=self._aware(self.MONDAY, time(8)), end=self._aware(self.MONDAY, time(9)))
        response = self.client.post(reverse("schedule_session_reschedule", args=[bookings[1].pk]), {
            "supervisor_id": self.supervisor.pk, "date": self.MONDAY.isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        bookings[1].refresh_from_db()
        self.assertEqual(bookings[1].planned_start, self._aware(self.MONDAY, time(10)))
        self.assertEqual(set(bookings[1].resources.values_list("resource_id", flat=True)),
                         {machine.pk, self.resource.pk})

    def test_api_can_reverse_adjacent_parts_without_unique_slot_collision(self):
        bookings, machine = self.split_bookings()
        response = self.client.post(reverse("schedule_sessions_reorder_day"), {
            "supervisor_id": self.supervisor.pk, "employee_id": self.worker.pk,
            "date": self.MONDAY.isoformat(), "session_ids": [bookings[1].pk, bookings[0].pk],
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        bookings[0].refresh_from_db()
        bookings[1].refresh_from_db()
        self.assertEqual(bookings[1].planned_start, self._aware(self.MONDAY, time(9)))
        self.assertEqual(bookings[0].planned_start, self._aware(self.MONDAY, time(10)))

    def split_bookings(self):
        machine = resource_for(make_machine(self.company))
        original = self._session(company=self.company, planned_end=self._aware(self.MONDAY, time(11)))
        sync_session_resources(original, [self.resource.pk, machine.pk])
        result = StageBookingSplittingService.split_booking(
            original.pk, expected_start=original.planned_start, expected_end=original.planned_end,
            cut_at=self._aware(self.MONDAY, time(10)),
        )
        self.assertTrue(result.ok, result.conflicts)
        return result.sessions, machine

    def test_board_returns_both_parts_with_operator_not_machine_identity(self):
        bookings, machine = self.split_bookings()
        response = self.client.get(reverse("schedule_board"), {"week_start": self.MONDAY.isoformat()})
        self.assertEqual(response.status_code, 200)
        rows = response.json()["sessions"]
        self.assertEqual({row["id"] for row in rows}, {s.pk for s in bookings})
        self.assertEqual([row["employee_id"] for row in rows], [self.worker.pk] * 2)
        self.assertTrue(all(row["employee_ids"] == [self.worker.pk] for row in rows))
        self.assertTrue(self.ticket.items.get().assigned_employees.filter(pk=self.worker.pk).exists())

    def test_reschedule_preserves_machine_and_does_not_move_other_part(self):
        bookings, machine = self.split_bookings()
        tail = bookings[1]
        response = self.client.post(reverse("schedule_session_reschedule", args=[tail.pk]), {
            "supervisor_id": self.supervisor.pk, "date": self.MONDAY.isoformat(),
            "start_time": "14:00", "end_time": "15:00",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        tail.refresh_from_db()
        bookings[0].refresh_from_db()
        self.assertEqual(tail.primary_resource_id, machine.pk)
        self.assertEqual(set(tail.resources.values_list("resource_id", flat=True)), {machine.pk, self.resource.pk})
        self.assertEqual(bookings[0].planned_end, self._aware(self.MONDAY, time(10)))
        self.assertEqual(response.json()["session"]["employee_id"], self.worker.pk)

    def test_api_cannot_move_continuation_over_first_part(self):
        bookings, _ = self.split_bookings()
        response = self.client.post(reverse("schedule_session_reschedule", args=[bookings[1].pk]), {
            "supervisor_id": self.supervisor.pk, "employee_id": self.supervisor.pk,
            "date": self.MONDAY.isoformat(), "start_time": "09:30", "end_time": "10:30",
        }, format="json")
        self.assertEqual(response.status_code, 409)
        bookings[1].refresh_from_db()
        self.assertEqual(bookings[1].planned_start, self._aware(self.MONDAY, time(10)))

    def test_terminal_chooses_next_booking_without_losing_past_plan_fallback(self):
        bookings, machine = self.split_bookings()
        tail = bookings[1]
        tail.planned_start += timedelta(days=7)
        tail.planned_end += timedelta(days=7)
        tail.save()
        with patch("django.utils.timezone.localdate", return_value=self.MONDAY):
            index = _build_planned_index(self.worker, [self.ticket.pk])
        self.assertEqual(index[(self.ticket.pk, "corte")].date, self.MONDAY.isoformat())
        with patch("django.utils.timezone.localdate", return_value=self.MONDAY + timedelta(days=1)):
            index = _build_planned_index(self.worker, [self.ticket.pk])
        self.assertEqual(index[(self.ticket.pk, "corte")].date, (self.MONDAY + timedelta(days=7)).isoformat())
        self.assertEqual(PlannedProductionSession.objects.for_employee(self.worker).count(), 2)
