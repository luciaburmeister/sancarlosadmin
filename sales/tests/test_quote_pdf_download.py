from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model


class QuotePdfDownloadViewTest(TestCase):
    def test_view_queues_the_render_and_sends_the_user_to_the_status_page(self):
        from sales.models import Quote
        from company.models import Company

        User = get_user_model()
        admin = User.objects.create_superuser(
            username="quote_pdf_admin", email="s@s.com", password="x"
        )
        company = Company.objects.create(name="PDF Test Co")
        quote = Quote.objects.create(company=company, project_name="PDF Test")

        self.client.force_login(admin)
        url = reverse("admin:sales_quote_download_pdf", args=[quote.pk])

        with patch("core.tasks.render_document_task.delay") as delay:
            delay.return_value.id = "task-1"
            resp = self.client.get(url)

        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("core_document_status"), resp.url)
        self.assertIn("task_id=task-1", resp.url)
        kwargs = delay.call_args.kwargs
        self.assertEqual(kwargs["renderer"], "sales.documents.render_quote_pdf")
        self.assertEqual(kwargs["kwargs"], {"quote_id": quote.pk})
        self.assertEqual(kwargs["filename"], f"Presupuesto_{quote.pk}.pdf")

    def test_renderer_uses_the_consolidated_quotation_template(self):
        from sales.documents import render_quote_pdf
        from sales.models import Quote
        from company.models import Company

        company = Company.objects.create(name="PDF Test Co 2")
        quote = Quote.objects.create(company=company, project_name="PDF Test 2")

        with patch(
            "sales.documents.PDFService.render_object_pdf", return_value=b"%PDF-1.4 fake",
        ) as mocked:
            self.assertEqual(render_quote_pdf(quote.pk), b"%PDF-1.4 fake")
        self.assertEqual(mocked.call_args.args[0], quote)
        self.assertEqual(mocked.call_args.args[1], "consolidated_quotation")

    def test_changeform_renders_actions_dropdown(self):
        from sales.models import Quote
        from company.models import Company

        User = get_user_model()
        admin = User.objects.create_superuser(
            username="quote_pdf_admin3", email="s3@s.com", password="x"
        )
        company = Company.objects.create(name="PDF Test Co 3")
        quote = Quote.objects.create(company=company, project_name="PDF Test 3")

        self.client.force_login(admin)
        resp = self.client.get(
            reverse("admin:sales_quote_change", args=[quote.pk])
        )

        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn(
            reverse("admin:sales_quote_export_quote_detail_action", args=[quote.pk]),
            content,
        )
        self.assertIn(
            reverse("admin:sales_quote_download_pdf_detail_action", args=[quote.pk]),
            content,
        )
