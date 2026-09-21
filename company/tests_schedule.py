from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from company.models import Company, CompanyScheduleSlot, WeekdayChoices


class CompanyWorkingWindowsForDayTest(TestCase):
    # 2026-09-14 is a Monday.
    MONDAY = date(2026, 9, 14)

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")

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

    def test_returns_both_windows_for_split_shift_day(self):
        windows = self.company.working_windows_for_day(self.MONDAY)
        self.assertEqual(windows, [(time(8, 0), time(12, 0)), (time(13, 0), time(17, 0))])

    def test_returns_empty_for_closed_day(self):
        tuesday = self.MONDAY + timedelta(days=1)
        self.assertEqual(self.company.working_windows_for_day(tuesday), [])

    def test_within_working_hours_does_not_raise(self):
        self.company.assert_within_working_hours(
            self._aware(self.MONDAY, time(9, 0)), self._aware(self.MONDAY, time(10, 0)),
        )

    def test_outside_working_hours_raises(self):
        with self.assertRaises(ValueError):
            self.company.assert_within_working_hours(
                self._aware(self.MONDAY, time(12, 30)), self._aware(self.MONDAY, time(12, 45)),
            )
