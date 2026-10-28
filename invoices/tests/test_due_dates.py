"""
Tests for `compute_due_date`: issue date + payment terms, moved to the
client's next payment day of the month when it has any.
"""
from datetime import date

from django.test import SimpleTestCase

from invoices.due_dates import compute_due_date


class ComputeDueDateTests(SimpleTestCase):
    def test_without_payment_days_is_issue_date_plus_terms(self):
        self.assertEqual(compute_due_date(date(2026, 1, 15), 30), date(2026, 2, 14))
        self.assertEqual(compute_due_date(date(2026, 1, 15), 0, []), date(2026, 1, 15))

    def test_moves_to_payment_day_later_in_same_month(self):
        self.assertEqual(compute_due_date(date(2026, 1, 15), 0, [25]), date(2026, 1, 25))

    def test_rolls_over_to_next_month(self):
        self.assertEqual(compute_due_date(date(2026, 1, 26), 0, [25]), date(2026, 2, 25))

    def test_terms_are_applied_before_the_payment_day(self):
        self.assertEqual(compute_due_date(date(2026, 1, 15), 30, [25]), date(2026, 2, 25))

    def test_base_date_on_a_payment_day_is_kept(self):
        self.assertEqual(compute_due_date(date(2026, 1, 25), 0, [25]), date(2026, 1, 25))

    def test_picks_the_first_of_several_payment_days(self):
        self.assertEqual(compute_due_date(date(2026, 1, 15), 30, [25, 10]), date(2026, 2, 25))
        self.assertEqual(compute_due_date(date(2026, 1, 26), 0, [10, 25]), date(2026, 2, 10))

    def test_day_past_month_end_counts_as_last_day(self):
        self.assertEqual(compute_due_date(date(2026, 2, 10), 0, [31]), date(2026, 2, 28))
        self.assertEqual(compute_due_date(date(2028, 2, 10), 0, [30]), date(2028, 2, 29))
        self.assertEqual(compute_due_date(date(2026, 4, 1), 0, [31]), date(2026, 4, 30))

    def test_rolls_over_the_year(self):
        self.assertEqual(compute_due_date(date(2026, 12, 26), 0, [25]), date(2027, 1, 25))
