from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from production.models import ProductionItemSessionKind, ProductionItemSessionStatus
from production.services.actual_stage_times import compute_actual_stage_times
from production.tests.factories import make_company, make_employee, make_item, make_item_session, make_product, make_ticket


class ComputeActualStageTimesTest(TestCase):
    STAGE = "corte"

    def setUp(self):
        self.company = make_company(stages=[self.STAGE])
        self.product = make_product(self.company, name="Ventana", stages=[self.STAGE])
        self.jose = make_employee(self.company, first_name="Jose", last_name="Murillo", stages=[])
        self.maria = make_employee(self.company, first_name="Maria", last_name="Garcia", stages=[])
        self.base = datetime(2026, 7, 1, 10, 0, tzinfo=dt_timezone.utc)

    def _make_ticket_with_item(self):
        ticket = make_ticket(self.company, stages=[self.STAGE], product=self.product, bom=False)
        return ticket, make_item(ticket, stages=[self.STAGE])

    def _make_session(self, item, start_min, end_min, employee, **overrides):
        end = overrides.pop("end_time", self.base + timedelta(minutes=end_min))
        overrides.setdefault("kind", ProductionItemSessionKind.ACTIVITY_STARTED)
        return make_item_session(
            item,
            stage=self.STAGE,
            employee=employee,
            start=self.base + timedelta(minutes=start_min),
            end=end,
            **overrides,
        )

    def test_merges_overlapping_sessions_and_averages_per_order(self):
        # Order 1: two workers overlap 10:00-11:00 and 10:30-11:30 -> 90 min
        _, item1 = self._make_ticket_with_item()
        self._make_session(item1, 0, 60, self.jose)
        self._make_session(item1, 30, 90, self.maria)

        # Order 2: single worker, 30 min
        _, item2 = self._make_ticket_with_item()
        self._make_session(item2, 0, 30, self.jose)

        actuals = compute_actual_stage_times(self.product)

        self.assertIn(self.STAGE, actuals)
        stats = actuals[self.STAGE]
        self.assertEqual(stats.order_count, 2)
        self.assertEqual(stats.avg_minutes, 60.0)
        self.assertEqual(stats.min_minutes, 30.0)
        self.assertEqual(stats.max_minutes, 90.0)
        self.assertEqual(stats.avg_employees, 1.5)
        self.assertEqual(stats.excluded_count, 0)

    def test_excludes_outlier_orders_from_all_averages(self):
        # Four normal orders (~30 min, 1 worker) and one forgotten session
        # left open for 10 hours with a second worker attributed.
        for _ in range(2):
            _, item = self._make_ticket_with_item()
            self._make_session(item, 0, 28, self.jose)
        for _ in range(2):
            _, item = self._make_ticket_with_item()
            self._make_session(item, 0, 32, self.jose)

        _, outlier_item = self._make_ticket_with_item()
        self._make_session(outlier_item, 0, 600, self.jose)
        self._make_session(outlier_item, 0, 600, self.maria)

        stats = compute_actual_stage_times(self.product)[self.STAGE]

        self.assertEqual(stats.excluded_count, 1)
        self.assertEqual(stats.order_count, 4)
        self.assertEqual(stats.avg_minutes, 30.0)
        self.assertEqual(stats.max_minutes, 32.0)
        # The outlier's 2-worker attribution must not skew the worker average
        self.assertEqual(stats.avg_employees, 1.0)

    def test_ignores_incomplete_and_cancelled_sessions(self):
        _, item = self._make_ticket_with_item()
        self._make_session(
            item, 0, 60, self.jose, status=ProductionItemSessionStatus.CANCELLED
        )
        self._make_session(
            item,
            0,
            0,
            self.jose,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            end_time=None,
        )

        actuals = compute_actual_stage_times(self.product)
        self.assertEqual(actuals, {})

    def test_only_counts_sessions_for_the_given_product(self):
        other_product = make_product(self.company, name="Puerta", stages=[self.STAGE])
        ticket = make_ticket(self.company, stages=[self.STAGE], product=other_product, bom=False)
        item = make_item(ticket, stages=[self.STAGE], planning={})
        self._make_session(item, 0, 45, self.jose)

        actuals = compute_actual_stage_times(self.product)
        self.assertEqual(actuals, {})
