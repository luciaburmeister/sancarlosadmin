from datetime import datetime, timedelta, timezone as dt_timezone

from django.test import TestCase

from configurator.models import Configuration
from management.models import ProjectFile
from production.models import ProductionItemSession
from production.tests.factories import (
    make_company,
    make_employee,
    make_item,
    make_item_session,
    make_product,
    make_ticket,
)
from sales.models import Client, Quote


class ForProjectFileQuerySetTest(TestCase):
    STAGE = "corte"

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(stages=[cls.STAGE])
        cls.product = make_product(cls.company, name="Ventana", stages=[cls.STAGE])
        cls.employee = make_employee(cls.company, first_name="Jose", last_name="Murillo", stages=[])
        cls.client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(company=cls.company, name="PF1", client=cls.client)
        cls.other_project_file = ProjectFile.objects.create(company=cls.company, name="PF2", client=cls.client)

        base = datetime(2026, 7, 1, 10, 0, tzinfo=dt_timezone.utc)

        def worked(ticket, minutes):
            item = make_item(ticket, stages=[cls.STAGE], planning={})
            return make_item_session(
                item, stage=cls.STAGE, employee=cls.employee,
                start=base, end=base + timedelta(minutes=minutes),
            )

        # Ticket directly linked to the project file.
        direct_ticket = make_ticket(
            cls.company, stages=[cls.STAGE], product=cls.product, bom=False,
            project_file=cls.project_file,
        )
        cls.direct_session = worked(direct_ticket, 60)

        # Legacy ticket without a direct FK — resolved via Configuration.quote.project_file.
        quote = Quote.objects.create(company=cls.company, client=cls.client, project_file=cls.project_file)
        fallback_config = Configuration.objects.create(company=cls.company, product=cls.product, quote=quote)
        fallback_ticket = make_ticket(cls.company, configuration=fallback_config, bom=False)
        cls.fallback_session = worked(fallback_ticket, 120)

        # Unrelated ticket for a different project file — must be excluded.
        other_ticket = make_ticket(
            cls.company, stages=[cls.STAGE], product=cls.product, bom=False,
            project_file=cls.other_project_file,
        )
        worked(other_ticket, 30)

    def test_includes_direct_and_fallback_tickets_excludes_unrelated(self):
        sessions = ProductionItemSession.objects.for_project_file(self.project_file)
        self.assertCountEqual(sessions, [self.direct_session, self.fallback_session])
