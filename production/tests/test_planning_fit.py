from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from production.services.production_planning import ProductionPlanningService
from production.services.working_calendar import WorkingCalendar


class PlanningFitTest(SimpleTestCase):
    def test_fitting_work_across_dst_preserves_elapsed_working_minutes(self):
        tz = ZoneInfo("Europe/Madrid")
        calendar = WorkingCalendar({6: [(time(1), time(5))]}, tz=tz)
        for month, day in ((3, 29), (10, 25)):
            with self.subTest(month=month):
                proposed = datetime(2026, month, day, 1, tzinfo=tz)
                start, end = ProductionPlanningService._fit(calendar, proposed, 120)
                self.assertEqual(calendar.working_minutes_between(start, end), 120)

    def test_fitting_a_short_booking_does_not_build_a_year_of_windows(self):
        class CountingCalendar(WorkingCalendar):
            calls = 0

            def windows_for_date(self, day):
                self.calls += 1
                return super().windows_for_date(day)

        tz = ZoneInfo("Europe/Madrid")
        calendar = CountingCalendar({0: [(time(8), time(18))]}, tz=tz)
        start, end = ProductionPlanningService._fit(calendar, datetime(2026, 9, 14, 9, tzinfo=tz), 60)
        self.assertEqual(end.hour, 10)
        self.assertLessEqual(calendar.calls, 4)
