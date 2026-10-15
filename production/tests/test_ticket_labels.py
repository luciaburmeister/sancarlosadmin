from django.test import TestCase

from configurator.models import Configuration
from management.models import ProjectFile
from production.services.ticket_labels import build_ticket_labels
from production.tests.factories import make_company, make_product, make_ticket
from sales.models import Client, Quote


class BuildTicketLabelsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(stages=[])
        cls.product = make_product(cls.company, name="Ventana", stages=[])
        cls.client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="PF1", client=cls.client, reference="EXP-0001",
        )

        cls.quote = Quote.objects.create(
            company=cls.company, client=cls.client, project_name="Quoted Project",
        )
        cls.config_with_quote = Configuration.objects.create(
            company=cls.company, product=cls.product, name="Config A",
            notes="some notes", quote=cls.quote,
        )
        cls.ticket_with_quote = make_ticket(
            cls.company, configuration=cls.config_with_quote, bom=False, project_file=cls.project_file,
        )

        cls.config_no_quote = Configuration.objects.create(
            company=cls.company, product=cls.product, name="Config B",
        )
        cls.ticket_no_quote = make_ticket(cls.company, configuration=cls.config_no_quote, bom=False)

    def test_uses_quote_project_name_when_present(self):
        labels = build_ticket_labels(
            ticket=self.ticket_with_quote,
            config=self.config_with_quote,
            project_file=self.ticket_with_quote.resolved_project_file,
        )
        self.assertEqual(labels.config_id, self.config_with_quote.id)
        self.assertEqual(labels.config_name, "Config A")
        self.assertEqual(labels.config_notes, "some notes")
        self.assertEqual(labels.quote_name, "Quoted Project")
        self.assertEqual(labels.template_name, "Ventana")
        self.assertEqual(labels.ticket_status, self.ticket_with_quote.status)
        self.assertEqual(
            labels.config_created_at, self.config_with_quote.created_at.date().isoformat()
        )
        self.assertEqual(labels.expediente, {
            "id": self.project_file.id,
            "reference": "EXP-0001",
            "name": "PF1",
            "client_name": "Acme",
            "start_date": self.project_file.start_date.strftime("%Y-%m-%d")
            if hasattr(self.project_file.start_date, "strftime")
            else self.project_file.start_date,
        })

    def test_falls_back_to_config_name_without_quote(self):
        labels = build_ticket_labels(
            ticket=self.ticket_no_quote,
            config=self.config_no_quote,
            project_file=self.ticket_no_quote.resolved_project_file,
        )
        self.assertEqual(labels.quote_name, "Config B")

    def test_no_project_file_gives_none_expediente(self):
        labels = build_ticket_labels(
            ticket=self.ticket_no_quote,
            config=self.config_no_quote,
            project_file=None,
        )
        self.assertIsNone(labels.expediente)

    def test_no_config_gives_empty_labels_and_falls_back_to_ticket_created_at(self):
        labels = build_ticket_labels(
            ticket=self.ticket_no_quote, config=None, project_file=None,
        )
        self.assertIsNone(labels.config_id)
        self.assertEqual(labels.config_name, "")
        self.assertEqual(labels.quote_name, "")
        self.assertEqual(
            labels.config_created_at, self.ticket_no_quote.created_at.date().isoformat()
        )
