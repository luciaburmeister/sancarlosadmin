from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company, Employee
from configurator.models import Configuration, ProductTemplate
from management.models import ProjectFile
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionTicket,
)
from sales.models import Client


class ProjectFileProductionHoursSummaryTest(TestCase):
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
        cls.client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(company=cls.company, name="PF1", client=cls.client)

        config = Configuration.objects.create(company=cls.company, product=cls.product)
        ct = ContentType.objects.get_for_model(Configuration)
        ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=config.id, project_file=cls.project_file,
        )
        item = ProductionItem.objects.create(
            ticket=ticket, company=cls.company, planning={}, production_stages_order=[cls.STAGE],
        )

        base = datetime(2026, 7, 1, 10, 0, tzinfo=dt_timezone.utc)
        cls.jose_session = ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=base, end_time=base + timedelta(hours=1), created_by=cls.jose,
        )
        cls.maria_session = ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=base, end_time=base + timedelta(minutes=30), created_by=cls.maria,
        )
        # Cancelled and in-progress sessions must not count toward totals.
        ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.CANCELLED,
            start_time=base, end_time=base + timedelta(hours=5), created_by=cls.jose,
        )
        ProductionItemSession.objects.create(
            production_item=item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            start_time=base, end_time=None, created_by=cls.maria,
        )

    def test_totals_per_employee_and_open_sessions_count(self):
        summary = self.project_file.production_hours_summary()

        self.assertEqual(summary["total_seconds"], 3600 + 1800)
        self.assertEqual(summary["open_sessions"], 1)

        by_id = {e["employee"].id: e for e in summary["employees"]}
        self.assertEqual(by_id[self.jose.id]["total_seconds"], 3600)
        self.assertEqual(by_id[self.jose.id]["sessions"], [self.jose_session])
        self.assertEqual(by_id[self.maria.id]["total_seconds"], 1800)
        self.assertEqual(by_id[self.maria.id]["sessions"], [self.maria_session])

        # Ranked most-worked first.
        self.assertEqual(summary["employees"][0]["employee"], self.jose)
