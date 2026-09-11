"""The admin template preview shows the real PDF, and reports template errors instead of 500s."""
import base64
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from company.models import Company
from configurator.models import ConfigurationExportTemplate
from sales.models import Quote

FAKE_PDF = b"%PDF-1.7 fake"


class ExportTemplatePreviewViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_superuser(username="preview_admin", email="p@p.com", password="x")
        cls.company = Company.objects.create(name="Preview Co")
        cls.quote = Quote.objects.create(company=cls.company, project_name="Preview quote")
        cls.template = ConfigurationExportTemplate.objects.create(
            name="Quote preview",
            kind="consolidated_quotation",
            company=cls.company,
            html="<div class='doc-container'><h1>{{ quote.project_name }}</h1></div>",
            css="h1 { color: red; }",
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_get_returns_inline_pdf(self):
        url = reverse("admin:configurator_export_template_preview", args=[self.template.pk])
        with patch("core.services.pdf_service.PDFService.html_to_pdf", return_value=FAKE_PDF) as to_pdf:
            resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])
        self.assertEqual(resp.content, FAKE_PDF)
        rendered_html = to_pdf.call_args.args[0]
        self.assertIn("Preview quote", rendered_html)
        self.assertIn("@page", rendered_html)

    def test_get_html_format_returns_intermediate_html(self):
        url = reverse("admin:configurator_export_template_preview", args=[self.template.pk])
        resp = self.client.get(url, {"format": "html"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/html"))
        self.assertIn("Preview quote", resp.content.decode())

    def test_live_post_returns_pdf_and_context_as_json(self):
        url = reverse("admin:configurator_export_template_preview_live")
        with patch("core.services.pdf_service.PDFService.html_to_pdf", return_value=FAKE_PDF):
            resp = self.client.post(url, {
                "html": "<div><h1>{{ quote.project_name }}</h1></div>",
                "css": "h1 { color: blue; }",
                "kind": "consolidated_quotation",
                "company": self.company.pk,
                "object_id": self.quote.pk,
            })

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(base64.b64decode(data["pdf"]), FAKE_PDF)
        self.assertIsInstance(data["context"], dict)
        self.assertIn("quote", data["context"])

    def test_live_post_reports_template_errors(self):
        url = reverse("admin:configurator_export_template_preview_live")
        resp = self.client.post(url, {
            "html": "{% if %}broken",
            "css": "",
            "kind": "consolidated_quotation",
            "company": self.company.pk,
            "object_id": self.quote.pk,
        })

        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.content)
        self.assertIn("Template error", data["error"])

    def test_live_post_reports_missing_weasyprint(self):
        url = reverse("admin:configurator_export_template_preview_live")
        with patch(
            "core.services.pdf_service.PDFService.html_to_pdf",
            side_effect=RuntimeError("PDF generation is unavailable"),
        ):
            resp = self.client.post(url, {
                "html": "<p>x</p>",
                "kind": "consolidated_quotation",
                "company": self.company.pk,
                "object_id": self.quote.pk,
            })

        self.assertEqual(resp.status_code, 503)
        self.assertIn("unavailable", json.loads(resp.content)["error"])
