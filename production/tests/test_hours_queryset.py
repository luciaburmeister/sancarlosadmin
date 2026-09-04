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
from sales.models import Client, Quote


class ForProjectFileQuerySetTest(TestCase):
    STAGE = "corte"

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", production_stages=[cls.STAGE])
        cls.product = ProductTemplate.objects.create(
            name="Ventana", company=cls.company, production_stages=[cls.STAGE],
        )
        cls.employee = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(company=cls.company, name="PF1", client=cls.client)
        cls.other_project_file = ProjectFile.objects.create(company=cls.company, name="PF2", client=cls.client)

        config_ct = ContentType.objects.get_for_model(Configuration)
        base = datetime(2026, 7, 1, 10, 0, tzinfo=dt_timezone.utc)

        # Ticket directly linked to the project file.
        direct_config = Configuration.objects.create(company=cls.company, product=cls.product)
        direct_ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=config_ct, object_id=direct_config.id,
            project_file=cls.project_file,
        )
        direct_item = ProductionItem.objects.create(
            ticket=direct_ticket, company=cls.company, planning={}, production_stages_order=[cls.STAGE],
        )
        cls.direct_session = ProductionItemSession.objects.create(
            production_item=direct_item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=base, end_time=base + timedelta(hours=1), created_by=cls.employee,
        )

        # Legacy ticket without a direct FK — resolved via Configuration.quote.project_file.
        quote = Quote.objects.create(company=cls.company, client=cls.client, project_file=cls.project_file)
        fallback_config = Configuration.objects.create(company=cls.company, product=cls.product, quote=quote)
        fallback_ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=config_ct, object_id=fallback_config.id,
        )
        fallback_item = ProductionItem.objects.create(
            ticket=fallback_ticket, company=cls.company, planning={}, production_stages_order=[cls.STAGE],
        )
        cls.fallback_session = ProductionItemSession.objects.create(
            production_item=fallback_item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=base, end_time=base + timedelta(hours=2), created_by=cls.employee,
        )

        # Unrelated ticket for a different project file — must be excluded.
        other_config = Configuration.objects.create(company=cls.company, product=cls.product)
        other_ticket = ProductionTicket.objects.create(
            company=cls.company, content_type=config_ct, object_id=other_config.id,
            project_file=cls.other_project_file,
        )
        other_item = ProductionItem.objects.create(
            ticket=other_ticket, company=cls.company, planning={}, production_stages_order=[cls.STAGE],
        )
        ProductionItemSession.objects.create(
            production_item=other_item, company=cls.company, production_stage=cls.STAGE,
            status=ProductionItemSessionStatus.DONE,
            start_time=base, end_time=base + timedelta(minutes=30), created_by=cls.employee,
        )

    def test_includes_direct_and_fallback_tickets_excludes_unrelated(self):
        sessions = ProductionItemSession.objects.for_project_file(self.project_file)
        self.assertCountEqual(sessions, [self.direct_session, self.fallback_session])
