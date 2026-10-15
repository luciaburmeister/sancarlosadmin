from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from management.models import ProjectFile
from production.models import HoursGranularity
from production.tests.factories import (
    make_company,
    make_employee,
    make_item,
    make_item_session,
    make_product,
    make_ticket,
)
from production.views.production_hours_dashboard_view import ProductionHoursDashboardView
from sales.models import Client


class ProductionHoursDashboardAggregationTest(TestCase):
    STAGE = "corte"

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(stages=[cls.STAGE])
        cls.product = make_product(cls.company, name="Ventana", stages=[cls.STAGE])
        cls.jose = make_employee(cls.company, first_name="Jose", last_name="Murillo", stages=[])
        cls.maria = make_employee(cls.company, first_name="Maria", last_name="Garcia", stages=[])

        client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="PF1", client=client, reference="EXP-0001",
        )

        ticket = make_ticket(
            cls.company, stages=[cls.STAGE], product=cls.product, bom=False,
            project_file=cls.project_file,
        )
        item = make_item(ticket, stages=[cls.STAGE], planning={})

        july_1 = datetime(2026, 7, 1, 9, 0, tzinfo=dt_timezone.utc)
        july_15 = datetime(2026, 7, 15, 9, 0, tzinfo=dt_timezone.utc)
        august_1 = datetime(2026, 8, 1, 9, 0, tzinfo=dt_timezone.utc)

        cls.s1 = make_item_session(
            item, stage=cls.STAGE, employee=cls.jose, start=july_1, end=july_1 + timedelta(hours=2),
        )
        cls.s2 = make_item_session(
            item, stage=cls.STAGE, employee=cls.maria, start=july_15, end=july_15 + timedelta(hours=1),
        )
        cls.s3 = make_item_session(
            item, stage=cls.STAGE, employee=cls.jose, start=august_1, end=august_1 + timedelta(hours=3),
        )

    def test_leaderboard_and_monthly_buckets_company_wide(self):
        # Without a date range the dashboard defaults to all time, which here
        # covers the same two months as the explicit range.
        for start, end in ((date(2026, 7, 1), date(2026, 8, 31)), (None, None)):
            with self.subTest(start=start, end=end):
                rows = ProductionHoursDashboardView._compute_leaderboard(
                    self.company, start, end,
                )
                by_id = {r["employee_id"]: r for r in rows}

                self.assertEqual(by_id[self.jose.id]["total_seconds"], 5 * 3600)
                self.assertEqual(by_id[self.jose.id]["sessions"], 2)
                self.assertEqual(by_id[self.maria.id]["total_seconds"], 3600)
                self.assertEqual(by_id[self.maria.id]["sessions"], 1)
                # Most-worked first.
                self.assertEqual(rows[0]["employee_id"], self.jose.id)

                buckets = ProductionHoursDashboardView._compute_buckets(
                    self.company, start, end, HoursGranularity.MONTH,
                )
                totals = [b["total_seconds"] for b in buckets]

                self.assertEqual(len(buckets), 2)
                self.assertEqual(totals, [3 * 3600, 3 * 3600])  # July: s1+s2, August: s3

    def test_buckets_filtered_by_employee(self):
        buckets = ProductionHoursDashboardView._compute_buckets(
            self.company, date(2026, 7, 1), date(2026, 8, 31), HoursGranularity.MONTH,
            employee_id=self.jose.id,
        )
        totals = [b["total_seconds"] for b in buckets]

        self.assertEqual(len(buckets), 2)
        self.assertEqual(totals, [2 * 3600, 3 * 3600])

    def test_day_sessions_include_the_expediente_each_belonged_to(self):
        sessions = ProductionHoursDashboardView._compute_day_sessions(
            self.company, date(2026, 7, 1),
        )

        self.assertEqual(len(sessions), 1)
        entry = sessions[0]
        self.assertEqual(entry["employee_name"], self.jose.full_name)
        self.assertEqual(entry["stage"], self.STAGE)
        self.assertEqual(entry["duration_label"], "2h 0m")
        self.assertEqual(entry["project_file"], self.project_file)

    def test_day_sessions_filtered_by_employee(self):
        sessions = ProductionHoursDashboardView._compute_day_sessions(
            self.company, date(2026, 7, 15), employee_id=self.jose.id,
        )
        self.assertEqual(sessions, [])
