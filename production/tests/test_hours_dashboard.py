from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company, Employee
from configurator.models import Configuration, ProductTemplate
from management.models import ProjectFile
from production.models import (
    HoursGranularity,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionTicket,
)
from production.views.production_hours_dashboard_view import ProductionHoursDashboardView
from sales.models import Client


class ProductionHoursDashboardAggregationTest(TestCase):
    STAGE = "corte"

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", production_stages=[cls.STAGE])
        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=[cls.STAGE],
        )
        cls.jose = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.maria = Employee.objects.create(
            company=cls.company, first_name="Maria", last_name="Garcia",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )

        client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="PF1", client=client, reference="EXP-0001",
        )

        config = Configuration.objects.create(company=cls.company, product=cls.product)
        ct = ContentType.objects.get_for_model(Configuration)
        ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=config.id, project_file=cls.project_file,
        )
        item = ProductionItem.objects.create(
            ticket=ticket, company=cls.company, planning={}, production_stages_order=[cls.STAGE],
        )

        july_1 = datetime(2026, 7, 1, 9, 0, tzinfo=dt_timezone.utc)
        july_15 = datetime(2026, 7, 15, 9, 0, tzinfo=dt_timezone.utc)
        august_1 = datetime(2026, 8, 1, 9, 0, tzinfo=dt_timezone.utc)

        cls.s1 = ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=july_1, end_time=july_1 + timedelta(hours=2), created_by=cls.jose,
        )
        cls.s2 = ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=july_15, end_time=july_15 + timedelta(hours=1), created_by=cls.maria,
        )
        cls.s3 = ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=august_1, end_time=august_1 + timedelta(hours=3), created_by=cls.jose,
        )

    def test_leaderboard_totals_per_employee(self):
        rows = ProductionHoursDashboardView._compute_leaderboard(
            self.company, date(2026, 7, 1), date(2026, 8, 31),
        )
        by_id = {r["employee_id"]: r for r in rows}

        self.assertEqual(by_id[self.jose.id]["total_seconds"], 5 * 3600)
        self.assertEqual(by_id[self.jose.id]["sessions"], 2)
        self.assertEqual(by_id[self.maria.id]["total_seconds"], 3600)
        self.assertEqual(by_id[self.maria.id]["sessions"], 1)
        # Most-worked first.
        self.assertEqual(rows[0]["employee_id"], self.jose.id)

    def test_buckets_by_month_company_wide(self):
        buckets = ProductionHoursDashboardView._compute_buckets(
            self.company, date(2026, 7, 1), date(2026, 8, 31), HoursGranularity.MONTH,
        )
        totals = [b["total_seconds"] for b in buckets]

        self.assertEqual(len(buckets), 2)
        self.assertEqual(totals, [3 * 3600, 3 * 3600])  # July: s1+s2, August: s3

    def test_leaderboard_and_buckets_default_to_all_time_without_a_date_range(self):
        rows = ProductionHoursDashboardView._compute_leaderboard(self.company, None, None)
        by_id = {r["employee_id"]: r for r in rows}
        self.assertEqual(by_id[self.jose.id]["total_seconds"], 5 * 3600)
        self.assertEqual(by_id[self.maria.id]["total_seconds"], 3600)

        buckets = ProductionHoursDashboardView._compute_buckets(
            self.company, None, None, HoursGranularity.MONTH,
        )
        self.assertEqual([b["total_seconds"] for b in buckets], [3 * 3600, 3 * 3600])

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
