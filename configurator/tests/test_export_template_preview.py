"""The admin template preview shows the real PDF, rendered on Celery, and reports template errors instead of 500s."""
import base64
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
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
        cache.clear()
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

    def _live_preview(self, data):
        """POST a live preview (the task runs eagerly in tests) and poll its result."""
        resp = self.client.post(reverse("admin:configurator_export_template_preview_live"), data)
        self.assertEqual(resp.status_code, 202)
        token = json.loads(resp.content)["token"]
        return self.client.get(reverse("admin:configurator_export_template_preview_result", args=[token]))

    def test_live_post_returns_pdf_and_context_as_json(self):
        with patch("core.services.pdf_service.PDFService.html_to_pdf", return_value=FAKE_PDF):
            resp = self._live_preview({
                "html": "<div><h1>{{ quote.project_name }}</h1></div>",
                "css": "h1 { color: blue; }",
                "kind": "consolidated_quotation",
                "company": self.company.pk,
                "object_id": self.quote.pk,
            })

        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["status"], "done")
        self.assertEqual(base64.b64decode(data["pdf"]), FAKE_PDF)
        self.assertIsInstance(data["context"], dict)
        self.assertIn("quote", data["context"])

    def test_live_post_does_not_render_in_the_request(self):
        url = reverse("admin:configurator_export_template_preview_live")
        with patch("configurator.tasks.render_export_template_preview.apply_async") as apply_async, \
                patch("core.services.pdf_service.PDFService.html_to_pdf") as to_pdf:
            resp = self.client.post(url, {"html": "<p>x</p>", "kind": "consolidated_quotation"})

        self.assertEqual(resp.status_code, 202)
        to_pdf.assert_not_called()
        token = json.loads(resp.content)["token"]
        self.assertEqual(apply_async.call_args.kwargs["task_id"], token)

        result_url = reverse("admin:configurator_export_template_preview_result", args=[token])
        self.assertEqual(json.loads(self.client.get(result_url).content), {"status": "pending"})

    def test_superseded_preview_is_not_rendered(self):
        from configurator.tasks import preview_latest_key, render_export_template_preview

        cache.set(preview_latest_key(self.admin.pk), "newer")
        with patch("core.services.pdf_service.PDFService.html_to_pdf") as to_pdf:
            render_export_template_preview("older", self.admin.pk, {"html": "<p>x</p>"})

        to_pdf.assert_not_called()
        url = reverse("admin:configurator_export_template_preview_result", args=["older"])
        self.assertEqual(json.loads(self.client.get(url).content), {"status": "superseded"})

    def test_result_is_private_to_its_user(self):
        User = get_user_model()
        other = User.objects.create_superuser(username="other_admin", email="o@p.com", password="x")
        with patch("core.services.pdf_service.PDFService.html_to_pdf", return_value=FAKE_PDF):
            resp = self.client.post(reverse("admin:configurator_export_template_preview_live"), {
                "html": "<p>x</p>",
                "kind": "consolidated_quotation",
                "company": self.company.pk,
                "object_id": self.quote.pk,
            })
        token = json.loads(resp.content)["token"]

        self.client.force_login(other)
        resp = self.client.get(reverse("admin:configurator_export_template_preview_result", args=[token]))
        self.assertEqual(resp.status_code, 404)

    def test_live_post_reports_template_errors(self):
        resp = self._live_preview({
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
        with patch(
            "core.services.pdf_service.PDFService.html_to_pdf",
            side_effect=RuntimeError("PDF generation is unavailable"),
        ):
            resp = self._live_preview({
                "html": "<p>x</p>",
                "kind": "consolidated_quotation",
                "company": self.company.pk,
                "object_id": self.quote.pk,
            })

        self.assertEqual(resp.status_code, 503)
        self.assertIn("unavailable", json.loads(resp.content)["error"])
