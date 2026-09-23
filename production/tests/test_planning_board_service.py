from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from company.models import Company, CompanyScheduleSlot, Employee, WeekdayChoices
from production.exceptions import PlannedSessionConflictError
from production.models import (
    PlannedProductionSession,
    PlannedProductionSessionSource,
    ProductionTicket,
)
from production.services import planning_board
from production.services.session_scheduler import get_or_create_resource_for_target


class PlanningBoardServiceTest(TestCase):
    # 2026-09-14 is a Monday.
    MONDAY = date(2026, 9, 14)

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.employee = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.other_employee = Employee.objects.create(
            company=cls.company, first_name="Maria", last_name="Garcia",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.resource = get_or_create_resource_for_target(cls.employee)

        # Split shift on Monday: 08:00-12:00 and 13:00-17:00.
        CompanyScheduleSlot.objects.create(
            company=cls.company, weekday=WeekdayChoices.MONDAY,
            start_time=time(8, 0), end_time=time(12, 0),
        )
        CompanyScheduleSlot.objects.create(
            company=cls.company, weekday=WeekdayChoices.MONDAY,
            start_time=time(13, 0), end_time=time(17, 0),
        )
        # Tuesday deliberately has no slots (closed day).

    def _aware(self, d: date, t: time) -> datetime:
        return timezone.make_aware(datetime.combine(d, t))

    # -- find_free_slot_in_day ----------------------------------------------------

    def test_finds_slot_in_first_window_when_free(self):
        slot = planning_board.find_free_slot_in_day(
            company=self.company, primary_resource_id=self.resource.id,
            day=self.MONDAY, duration=timedelta(hours=1),
        )
        self.assertIsNotNone(slot)
        start, end = slot
        self.assertEqual(timezone.localtime(start).time(), time(8, 0))
        self.assertEqual(timezone.localtime(end).time(), time(9, 0))

    def test_spills_to_second_window_when_first_is_full(self):
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(12, 0)),
            primary_resource=self.resource,
        )
        slot = planning_board.find_free_slot_in_day(
            company=self.company, primary_resource_id=self.resource.id,
            day=self.MONDAY, duration=timedelta(hours=1),
        )
        self.assertIsNotNone(slot)
        start, _ = slot
        self.assertEqual(timezone.localtime(start).time(), time(13, 0))

    def test_returns_none_when_no_free_slot_fits(self):
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(12, 0)),
            primary_resource=self.resource,
        )
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(13, 0)),
            planned_end=self._aware(self.MONDAY, time(17, 0)),
            primary_resource=self.resource,
        )
        slot = planning_board.find_free_slot_in_day(
            company=self.company, primary_resource_id=self.resource.id,
            day=self.MONDAY, duration=timedelta(hours=1),
        )
        self.assertIsNone(slot)

    # -- move_session ---------------------------------------------------------------

    def test_move_session_locked_raises(self):
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
            locked=True,
        )
        with self.assertRaises(ValueError):
            planning_board.move_session(
                company=self.company, session=session, employee=None, day=self.MONDAY,
            )

    def test_move_session_auto_snap_preserves_duration(self):
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 30)),
            primary_resource=self.resource,
        )
        # Occupy 08:00-09:30 for the OTHER employee's resource so moving there forces a snap search.
        other_resource = get_or_create_resource_for_target(self.other_employee)
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 30)),
            primary_resource=other_resource,
        )
        updated = planning_board.move_session(
            company=self.company, session=session, employee=self.other_employee, day=self.MONDAY,
        )
        self.assertEqual(updated.primary_resource_id, other_resource.id)
        # 1h30 duration preserved, snapped after the 08:00-09:30 block.
        self.assertEqual(timezone.localtime(updated.planned_start).time(), time(9, 30))
        self.assertEqual(timezone.localtime(updated.planned_end).time(), time(11, 0))

    def test_move_session_explicit_time_conflict_raises(self):
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=self._aware(self.MONDAY, time(10, 0)),
            planned_end=self._aware(self.MONDAY, time(11, 0)),
            primary_resource=self.resource,
        )
        with self.assertRaises(PlannedSessionConflictError):
            planning_board.move_session(
                company=self.company, session=session, employee=None, day=self.MONDAY,
                start_time=time(10, 30), end_time=time(11, 30),
            )

    def test_move_session_with_end_day_spans_multiple_days(self):
        wednesday = self.MONDAY + timedelta(days=2)
        CompanyScheduleSlot.objects.create(
            company=self.company, weekday=WeekdayChoices.WEDNESDAY,
            start_time=time(8, 0), end_time=time(17, 0),
        )
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        updated = planning_board.move_session(
            company=self.company, session=session, employee=None, day=self.MONDAY,
            start_time=time(9, 0), end_time=time(10, 0), end_day=wednesday,
        )
        self.assertEqual(timezone.localtime(updated.planned_start).date(), self.MONDAY)
        self.assertEqual(timezone.localtime(updated.planned_end).date(), wednesday)
        self.assertEqual(timezone.localtime(updated.planned_end).time(), time(10, 0))

    def test_move_session_without_end_day_stays_single_day(self):
        # end_day omitted — today's behavior: end_time is combined with the same day.
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        updated = planning_board.move_session(
            company=self.company, session=session, employee=None, day=self.MONDAY,
            start_time=time(9, 0), end_time=time(10, 0),
        )
        self.assertEqual(timezone.localtime(updated.planned_end).date(), self.MONDAY)

    def test_move_session_explicit_time_outside_hours_raises(self):
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        with self.assertRaises(ValueError):
            planning_board.move_session(
                company=self.company, session=session, employee=None, day=self.MONDAY,
                start_time=time(12, 15), end_time=time(12, 45),
            )

    # -- create_session ---------------------------------------------------------------

    def test_create_session_success(self):
        ticket = ProductionTicket.objects.create(company=self.company, content_type=self._config_ct(), object_id=1)
        session = planning_board.create_session(
            company=self.company, ticket=ticket, stage="corte", employee=self.employee,
            day=self.MONDAY, start_time=time(9, 0), end_time=time(10, 0),
        )
        self.assertEqual(session.source, PlannedProductionSessionSource.MANUAL)
        self.assertEqual(session.primary_resource_id, self.resource.id)
        self.assertEqual(timezone.localtime(session.planned_start).time(), time(9, 0))

    def test_create_session_conflict_raises(self):
        PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(10, 0)),
            primary_resource=self.resource,
        )
        ticket = ProductionTicket.objects.create(company=self.company, content_type=self._config_ct(), object_id=2)
        with self.assertRaises(PlannedSessionConflictError):
            planning_board.create_session(
                company=self.company, ticket=ticket, stage="corte", employee=self.employee,
                day=self.MONDAY, start_time=time(9, 30), end_time=time(10, 30),
            )

    def test_create_session_end_before_start_raises(self):
        ticket = ProductionTicket.objects.create(company=self.company, content_type=self._config_ct(), object_id=3)
        with self.assertRaises(ValueError):
            planning_board.create_session(
                company=self.company, ticket=ticket, stage="corte", employee=self.employee,
                day=self.MONDAY, start_time=time(10, 0), end_time=time(9, 0),
            )

    def test_create_session_with_end_day_spans_multiple_days(self):
        wednesday = self.MONDAY + timedelta(days=2)
        CompanyScheduleSlot.objects.create(
            company=self.company, weekday=WeekdayChoices.WEDNESDAY,
            start_time=time(8, 0), end_time=time(17, 0),
        )
        ticket = ProductionTicket.objects.create(company=self.company, content_type=self._config_ct(), object_id=6)
        session = planning_board.create_session(
            company=self.company, ticket=ticket, stage="corte", employee=self.employee,
            day=self.MONDAY, start_time=time(9, 0), end_time=time(10, 0), end_day=wednesday,
        )
        self.assertEqual(timezone.localtime(session.planned_start).date(), self.MONDAY)
        self.assertEqual(timezone.localtime(session.planned_end).date(), wednesday)
        self.assertEqual(timezone.localtime(session.planned_end).time(), time(10, 0))

    def test_create_session_without_time_auto_snaps(self):
        ticket = ProductionTicket.objects.create(company=self.company, content_type=self._config_ct(), object_id=4)
        session = planning_board.create_session(
            company=self.company, ticket=ticket, stage="corte", employee=self.employee, day=self.MONDAY,
        )
        self.assertEqual(timezone.localtime(session.planned_start).time(), time(8, 0))
        self.assertEqual(
            session.planned_end - session.planned_start, planning_board.DEFAULT_SESSION_DURATION,
        )

    def test_create_session_without_time_snaps_after_existing(self):
        # Dropping two backlog cards on the same day/employee in a row should
        # sequence them one after another — no time picker, order preserved.
        ticket = ProductionTicket.objects.create(company=self.company, content_type=self._config_ct(), object_id=5)
        first = planning_board.create_session(
            company=self.company, ticket=ticket, stage="corte", employee=self.employee, day=self.MONDAY,
        )
        second = planning_board.create_session(
            company=self.company, ticket=ticket, stage="pintura", employee=self.employee, day=self.MONDAY,
        )
        self.assertEqual(second.planned_start, first.planned_end)

    @staticmethod
    def _config_ct():
        from django.contrib.contenttypes.models import ContentType
        from configurator.models import Configuration
        return ContentType.objects.get_for_model(Configuration)

    # -- reorder_day_sessions ---------------------------------------------------------

    def test_reorder_day_sessions_swaps_order_preserving_durations(self):
        first = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        second = PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 30)),
            primary_resource=self.resource,
        )

        updated = planning_board.reorder_day_sessions(
            company=self.company, resource=self.resource, day=self.MONDAY,
            session_ids=[second.id, first.id],
        )

        by_id = {s.id: s for s in updated}
        # second (30min) now goes first, starting at the original anchor (08:00).
        self.assertEqual(timezone.localtime(by_id[second.id].planned_start).time(), time(8, 0))
        self.assertEqual(timezone.localtime(by_id[second.id].planned_end).time(), time(8, 30))
        # first (1h) follows immediately after.
        self.assertEqual(timezone.localtime(by_id[first.id].planned_start).time(), time(8, 30))
        self.assertEqual(timezone.localtime(by_id[first.id].planned_end).time(), time(9, 30))

    def test_reorder_day_sessions_spills_into_next_window(self):
        # Anchor at 11:30, first window closes at 12:00 — a 1h session won't
        # fit and must spill into the 13:00 window.
        a = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(11, 30)),
            planned_end=self._aware(self.MONDAY, time(11, 45)),
            primary_resource=self.resource,
        )
        b = PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=self._aware(self.MONDAY, time(11, 45)),
            planned_end=self._aware(self.MONDAY, time(12, 45)),
            primary_resource=self.resource,
        )

        updated = planning_board.reorder_day_sessions(
            company=self.company, resource=self.resource, day=self.MONDAY,
            session_ids=[a.id, b.id],
        )
        by_id = {s.id: s for s in updated}
        self.assertEqual(timezone.localtime(by_id[a.id].planned_start).time(), time(11, 30))
        self.assertEqual(timezone.localtime(by_id[a.id].planned_end).time(), time(11, 45))
        # b couldn't fit before 12:00 (would need until 12:45), so it spills to 13:00.
        self.assertEqual(timezone.localtime(by_id[b.id].planned_start).time(), time(13, 0))
        self.assertEqual(timezone.localtime(by_id[b.id].planned_end).time(), time(14, 0))

    def test_reorder_day_sessions_rejects_mismatched_set(self):
        first = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=self._aware(self.MONDAY, time(9, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 30)),
            primary_resource=self.resource,
        )
        with self.assertRaises(ValueError):
            planning_board.reorder_day_sessions(
                company=self.company, resource=self.resource, day=self.MONDAY,
                session_ids=[first.id],  # missing the second session for that day
            )

    def test_reorder_day_sessions_rejects_locked_session(self):
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
            locked=True,
        )
        with self.assertRaises(ValueError):
            planning_board.reorder_day_sessions(
                company=self.company, resource=self.resource, day=self.MONDAY,
                session_ids=[session.id],
            )

    def test_reorder_day_sessions_rejects_duplicate_ids(self):
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(8, 0)),
            planned_end=self._aware(self.MONDAY, time(9, 0)),
            primary_resource=self.resource,
        )
        with self.assertRaises(ValueError):
            planning_board.reorder_day_sessions(
                company=self.company, resource=self.resource, day=self.MONDAY,
                session_ids=[session.id, session.id],
            )

    def test_reorder_day_sessions_raises_when_hours_run_out(self):
        # A single 5-hour session anchored at 11:00 can't fit in either
        # window of the split shift (4h and 4h, but only ~1h + 4h remain).
        session = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=self._aware(self.MONDAY, time(11, 0)),
            planned_end=self._aware(self.MONDAY, time(16, 0)),
            primary_resource=self.resource,
        )
        with self.assertRaises(ValueError):
            planning_board.reorder_day_sessions(
                company=self.company, resource=self.resource, day=self.MONDAY,
                session_ids=[session.id],
            )

