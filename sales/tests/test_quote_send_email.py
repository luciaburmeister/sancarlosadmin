from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from company.models import Company
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from core.services.pdf_service import PDFService
from core.views.send_document_email_view import ClientRecipientsForm
from sales.models import Client, Quote
from sales.services.quote_email_service import QuoteEmailService
from sales.views.quote_send_email_view import QuoteSendEmailView


class QuoteSendEmailViewTemplatePickerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Acme")
        cls.other_company = Company.objects.create(name="Other Co")
        cls.quote = Quote.objects.create(company=cls.company, project_name="Q1")

        cls.generic_template = ConfigurationExportTemplate.objects.create(
            name="Generic", kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
        )
        cls.company_template = ConfigurationExportTemplate.objects.create(
            name="Acme specific",
            kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
            company=cls.company,
        )
        cls.other_company_template = ConfigurationExportTemplate.objects.create(
            name="Other Co specific",
            kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
            company=cls.other_company,
        )
        # A superuser sees every company by default, which is exactly the case
        # that must not leak another tenant's template into the picker.
        cls.superuser = get_user_model().objects.create(
            username="admin", is_superuser=True, is_staff=True,
        )

    def _view_for(self, user):
        view = QuoteSendEmailView()
        view.request = RequestFactory().get("/")
        view.request.user = user
        return view

    def test_available_templates_excludes_other_companies(self):
        templates = self._view_for(self.superuser).get_available_templates(self.quote)
        self.assertCountEqual(templates, [self.generic_template, self.company_template])

    def test_default_template_matches_export_template_kind_resolution(self):
        default_template = self._view_for(self.superuser).get_default_template(self.quote)
        self.assertEqual(default_template, self.company_template)


class ClientRecipientsFormTemplateFieldTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Acme")
        cls.template_a = ConfigurationExportTemplate.objects.create(
            name="A", kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
        )
        cls.template_b = ConfigurationExportTemplate.objects.create(
            name="B", kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
        )

    def test_template_field_absent_when_no_templates_available(self):
        form = ClientRecipientsForm(available_templates=None)
        self.assertNotIn("template", form.fields)

    def test_template_field_lists_choices_with_default_preselected(self):
        form = ClientRecipientsForm(
            available_templates=[self.template_a, self.template_b],
            initial={"template": self.template_a.pk},
        )
        self.assertEqual(
            form.fields["template"].choices,
            [(self.template_a.pk, "A"), (self.template_b.pk, "B")],
        )
        self.assertEqual(form.initial["template"], self.template_a.pk)

    def test_submitting_a_different_template_id_is_coerced_to_int(self):
        form = ClientRecipientsForm(
            data={"to_emails": [], "template": str(self.template_b.pk)},
            available_templates=[self.template_a, self.template_b],
        )
        form.is_valid()
        self.assertEqual(form.cleaned_data["template"], self.template_b.pk)


class QuoteEmailServiceTemplateOverrideTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Acme")
        cls.quote = Quote.objects.create(
            company=cls.company, project_name="Q1",
        )

    @patch.object(PDFService, "render_object_pdf", return_value=b"%PDF-fake")
    def test_send_quote_email_forwards_the_chosen_template_id(self, mock_render):
        chosen_template_id = 999
        QuoteEmailService.send_quote_email(
            self.quote, to_emails=["client@example.com"], template_id=chosen_template_id,
        )
        self.assertEqual(mock_render.call_args.kwargs["template_id"], chosen_template_id)

    @patch.object(PDFService, "render_object_pdf", return_value=b"%PDF-fake")
    def test_send_quote_email_without_a_choice_leaves_default_resolution(self, mock_render):
        QuoteEmailService.send_quote_email(self.quote, to_emails=["client@example.com"])
        self.assertIsNone(mock_render.call_args.kwargs["template_id"])


class QuoteSendEmailViewPostWiringTest(TestCase):
    """The one path worth a real request: the picker's choice must survive
    the POST -> Celery task hand-off untouched."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Acme")
        cls.client_obj = Client.objects.create(
            company=cls.company, name="Cliente SA", email="client@example.com",
        )
        cls.quote = Quote.objects.create(
            company=cls.company, client=cls.client_obj, project_name="Q1",
        )
        cls.template = ConfigurationExportTemplate.objects.create(
            name="Chosen template",
            kind=ExportTemplateKind.CONSOLIDATED_QUOTATION,
            company=cls.company,
        )
        cls.admin = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="secret",
        )

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("admin:sales_quote_send_email", args=[self.quote.pk])

    def test_post_forwards_the_selected_template_id_to_the_task(self):
        with patch(
            "sales.views.quote_send_email_view.send_quote_email_task"
        ) as task:
            self.client.post(
                self.url,
                {"to_emails": ["client@example.com"], "template": str(self.template.pk)},
            )
        task.delay.assert_called_once_with(
            self.quote.id,
            to_emails=["client@example.com"],
            cc_emails=["admin@example.com"],
            reply_to=["admin@example.com"],
            template_id=self.template.pk,
        )
