from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model


class QuotePdfDownloadViewTest(TestCase):
    def test_view_returns_pdf(self):
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

        with patch(
            "sales.views.quote_pdf_download_view.PDFService.render_object_pdf",
            return_value=b"%PDF-1.4 fake",
        ) as mocked:
            resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn(f"Presupuesto_{quote.pk}.pdf", resp["Content-Disposition"])
        self.assertEqual(mocked.call_args.args[1], "consolidated_quotation")

    def test_view_404s_without_template(self):
        from sales.models import Quote
        from company.models import Company

        User = get_user_model()
        admin = User.objects.create_superuser(
            username="quote_pdf_admin2", email="s2@s.com", password="x"
        )
        company = Company.objects.create(name="PDF Test Co 2")
        quote = Quote.objects.create(company=company, project_name="PDF Test 2")

        self.client.force_login(admin)
        url = reverse("admin:sales_quote_download_pdf", args=[quote.pk])

        with patch(
            "sales.views.quote_pdf_download_view.PDFService.render_object_pdf",
            return_value=None,
        ):
            resp = self.client.get(url)

        self.assertEqual(resp.status_code, 404)

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
