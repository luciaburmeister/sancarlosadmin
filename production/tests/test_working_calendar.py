from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from company.models import WeekdayChoices
from production.services.working_calendar import WorkingCalendar
from production.tests.factories import add_schedule, make_company

# 2026-09-11 is a Friday, 2026-09-14 the following Monday.
FRIDAY = date(2026, 9, 11)
SATURDAY = date(2026, 9, 12)
MONDAY = date(2026, 9, 14)

# Madrid DST transitions: 2026-03-29 02:00 -> 03:00, 2026-10-25 03:00 -> 02:00.
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)


class WorkingCalendarTest(TestCase):
    """The split-shift cases here are direct regressions on ScheduleContext.from_company,
    which collapsed all slots to max(start)..min(end) and produced an inverted window."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        add_schedule(cls.company)  # Mon-Fri, 08:00-14:00 and 15:00-18:00
        cls.tz = timezone.get_current_timezone()

    def cal(self):
        return WorkingCalendar.for_company(self.company)

    def at(self, day: date, hour: int, minute: int = 0) -> datetime:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)

    # --- windows ---------------------------------------------------------

    def test_split_shift_yields_two_windows_not_one_collapsed_range(self):
        windows = self.cal().windows_for_date(FRIDAY)

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].start, self.at(FRIDAY, 8))
        self.assertEqual(windows[0].end, self.at(FRIDAY, 14))
        self.assertEqual(windows[1].start, self.at(FRIDAY, 15))
        self.assertEqual(windows[1].end, self.at(FRIDAY, 18))
        # The old collapse produced start=15:00 > end=14:00; assert it cannot recur.
        for window in windows:
            self.assertLess(window.start, window.end)

    def test_rest_day_has_no_windows(self):
        self.assertEqual(self.cal().windows_for_date(SATURDAY), [])

    def test_abutting_slots_are_merged_into_one_window(self):
        company = make_company(name="Continuous Co")
        add_schedule(
            company,
            weekdays=[WeekdayChoices.FRIDAY],
            ranges=((time(8, 0), time(14, 0)), (time(14, 0), time(18, 0))),
        )

        windows = WorkingCalendar.for_company(company).windows_for_date(FRIDAY)

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].minutes, 10 * 60)

    def test_windows_between_clips_to_bounds(self):
        windows = self.cal().windows_between(self.at(FRIDAY, 12), self.at(FRIDAY, 16))

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].start, self.at(FRIDAY, 12))  # clipped, not 08:00
        self.assertEqual(windows[0].end, self.at(FRIDAY, 14))
        self.assertEqual(windows[1].end, self.at(FRIDAY, 16))  # clipped, not 18:00

    def test_windows_between_spans_the_weekend(self):
        windows = self.cal().windows_between(self.at(FRIDAY, 16), self.at(MONDAY, 10))

        # Friday afternoon remainder + Monday morning; Saturday and Sunday closed.
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].end, self.at(FRIDAY, 18))
        self.assertEqual(windows[1].start, self.at(MONDAY, 8))

    def test_empty_range_and_inverted_range_yield_nothing(self):
        cal = self.cal()
        self.assertEqual(cal.windows_between(self.at(FRIDAY, 10), self.at(FRIDAY, 10)), [])
        self.assertEqual(cal.windows_between(self.at(FRIDAY, 12), self.at(FRIDAY, 9)), [])

    # --- minutes ---------------------------------------------------------

    def test_working_minutes_exclude_the_lunch_break(self):
        # 13:00 -> 16:00 is 3 wall-clock hours but only 2 open hours.
        minutes = self.cal().working_minutes_between(self.at(FRIDAY, 13), self.at(FRIDAY, 16))
        self.assertEqual(minutes, 120)

    def test_working_minutes_over_a_full_day(self):
        minutes = self.cal().working_minutes_between(self.at(FRIDAY, 0), self.at(SATURDAY, 0))
        self.assertEqual(minutes, 9 * 60)

    def test_working_minutes_skip_closed_days(self):
        minutes = self.cal().working_minutes_between(self.at(SATURDAY, 0), self.at(MONDAY, 0))
        self.assertEqual(minutes, 0)

    # --- is_working / next_open ------------------------------------------

    def test_is_working(self):
        cal = self.cal()
        self.assertTrue(cal.is_working(self.at(FRIDAY, 9)))
        self.assertFalse(cal.is_working(self.at(FRIDAY, 14, 30)))  # lunch
        self.assertFalse(cal.is_working(self.at(SATURDAY, 9)))
        self.assertFalse(cal.is_working(self.at(FRIDAY, 18)))  # end is exclusive

    def test_next_open_jumps_the_weekend(self):
        self.assertEqual(
            self.cal().next_open(self.at(FRIDAY, 18, 1)),
            self.at(MONDAY, 8),
        )

    def test_next_open_jumps_the_lunch_break(self):
        self.assertEqual(
            self.cal().next_open(self.at(FRIDAY, 14, 30)),
            self.at(FRIDAY, 15),
        )

    def test_next_open_returns_the_moment_itself_when_already_open(self):
        moment = self.at(FRIDAY, 9, 17)
        self.assertEqual(self.cal().next_open(moment), moment)

    def test_next_open_is_none_without_a_schedule(self):
        cal = WorkingCalendar.for_company(make_company(name="Unscheduled Co"))

        self.assertFalse(cal.has_slots)
        self.assertIsNone(cal.next_open(self.at(FRIDAY, 9)))
        self.assertEqual(cal.windows_for_date(FRIDAY), [])

    # --- DST -------------------------------------------------------------

    def test_spring_forward_day_counts_real_elapsed_minutes(self):
        """A window spanning the 02:00->03:00 gap is one hour shorter than the clock says."""
        company = make_company(name="Night Co")
        add_schedule(
            company,
            weekdays=[WeekdayChoices.SUNDAY],
            ranges=((time(0, 0), time(6, 0)),),
        )
        cal = WorkingCalendar.for_company(company)

        windows = cal.windows_for_date(SPRING_FORWARD)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].minutes, 5 * 60)

    def test_fall_back_day_counts_real_elapsed_minutes(self):
        """A window spanning the repeated 02:00 hour is one hour longer."""
        company = make_company(name="Night Co 2")
        add_schedule(
            company,
            weekdays=[WeekdayChoices.SUNDAY],
            ranges=((time(0, 0), time(6, 0)),),
        )
        cal = WorkingCalendar.for_company(company)

        windows = cal.windows_for_date(FALL_BACK)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].minutes, 7 * 60)

    def test_business_hours_are_unaffected_by_dst(self):
        """Madrid transitions at 02:00-03:00, so a normal shift is always 9 hours."""
        company = make_company(name="Weekend Co")
        add_schedule(
            company,
            weekdays=[WeekdayChoices.SUNDAY],
            ranges=((time(8, 0), time(14, 0)), (time(15, 0), time(18, 0))),
        )
        cal = WorkingCalendar.for_company(company)

        for day in (SPRING_FORWARD, FALL_BACK):
            with self.subTest(day=day):
                self.assertEqual(sum(w.minutes for w in cal.windows_for_date(day)), 9 * 60)

    # --- window helpers --------------------------------------------------

    def test_window_clipped_returns_none_when_disjoint(self):
        window = self.cal().windows_for_date(FRIDAY)[0]  # 08:00-14:00
        self.assertIsNone(window.clipped(self.at(FRIDAY, 15), self.at(FRIDAY, 16)))

    def test_windows_are_chronological_across_days(self):
        windows = self.cal().windows_between(
            self.at(MONDAY, 0), self.at(MONDAY, 0) + timedelta(days=5)
        )
        starts = [w.start for w in windows]
        self.assertEqual(starts, sorted(starts))
