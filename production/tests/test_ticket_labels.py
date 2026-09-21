from datetime import date

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ProductTemplate
from management.models import ProjectFile
from production.models import ProductionTicket
from production.services.ticket_labels import build_ticket_labels
from sales.models import Client, Quote


class BuildTicketLabelsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.product = ProductTemplate.objects.create(name="Ventana", company=cls.company)
        cls.client = Client.objects.create(company=cls.company, name="Acme")
        cls.project_file = ProjectFile.objects.create(
            company=cls.company, name="PF1", client=cls.client, reference="EXP-0001",
        )
        ct = ContentType.objects.get_for_model(Configuration)

        cls.quote = Quote.objects.create(
            company=cls.company, client=cls.client, project_name="Quoted Project",
        )
        cls.config_with_quote = Configuration.objects.create(
            company=cls.company, product=cls.product, name="Config A",
            notes="some notes", quote=cls.quote,
        )
        cls.ticket_with_quote = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config_with_quote.id,
            project_file=cls.project_file,
        )

        cls.config_no_quote = Configuration.objects.create(
            company=cls.company, product=cls.product, name="Config B",
        )
        cls.ticket_no_quote = ProductionTicket.objects.create(
            company=cls.company, content_type=ct, object_id=cls.config_no_quote.id,
        )

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

    def test_no_config_gives_none_config_id(self):
        labels = build_ticket_labels(
            ticket=self.ticket_no_quote, config=None, project_file=None,
        )
        self.assertIsNone(labels.config_id)
        self.assertEqual(labels.config_name, "")
        self.assertEqual(labels.quote_name, "")

    def test_no_config_falls_back_to_ticket_created_at(self):
        labels = build_ticket_labels(
            ticket=self.ticket_no_quote, config=None, project_file=None,
        )
        self.assertEqual(
            labels.config_created_at, self.ticket_no_quote.created_at.date().isoformat()
        )
