from unittest.mock import patch

from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.documents import render_asset_transaction_document
from assets.models import AssetTransaction, TransactionTypeChoices
from company.models import Company
from configurator.models import (
    ConfigurationExportTemplate,
    ExportTemplateKind,
    ExportTemplateSubtype,
)
from configurator.services.export.asset_transaction import AssetTransactionExportStrategy
from core.documents import renderer_path
from core.tasks import render_document_task


@override_settings(BACKEND_URL="https://erp.example.com")
class AssetTransactionDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )
        cls.template = ConfigurationExportTemplate.objects.create(
            company=cls.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            name="Albarán",
            html="<p>{{ asset_name }}</p><img src=\"{{ document_qr }}\">",
        )

    def _path(self):
        return reverse("asset_transaction_document", args=[self.transaction.public_token])

    def _status(self, task_id="t"):
        return reverse("asset_transaction_document_status", args=[self.transaction.public_token, task_id])

    def _download(self, task_id="t"):
        return reverse("asset_transaction_document_download", args=[self.transaction.public_token, task_id])

    def _get_queued(self):
        with patch("core.tasks.render_document_task.delay") as delay:
            delay.return_value.id = "task-1"
            response = self.client.get(self._path())
        return response, delay

    def test_new_transaction_gets_a_public_document_url(self):
        self.assertTrue(self.transaction.public_token)
        self.assertEqual(self.transaction.document_url, "https://erp.example.com" + self._path())

    def test_export_context_qr_encodes_the_document_url(self):
        context = AssetTransactionExportStrategy(self.transaction).get_context()

        self.assertEqual(context["document_url"], self.transaction.document_url)
        self.assertTrue(context["document_qr"].startswith("data:image/png;base64,"))

    def test_public_document_is_queued_and_answers_with_the_status_page(self):
        response, delay = self._get_queued()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self._download("task-1"))
        kwargs = delay.call_args.kwargs
        self.assertEqual(kwargs["renderer"], renderer_path(render_asset_transaction_document))
        self.assertEqual(kwargs["kwargs"], {
            "transaction_id": self.transaction.pk,
            "template_id": self.template.pk,
            "document_url": "http://testserver" + self._path(),
        })
        self.assertEqual(kwargs["filename"], f"transaction_{self.transaction.pk}.pdf")
        self.assertTrue(kwargs["inline"])
        self.assertIsNone(kwargs["user_id"])

    def test_public_document_ignores_the_email_template(self):
        email = ConfigurationExportTemplate.objects.create(
            company=self.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            subtype=ExportTemplateSubtype.EMAIL,
            name="Email",
            html="<p>EMAIL-DOC</p>",
        )

        _, delay = self._get_queued()

        self.assertNotEqual(delay.call_args.kwargs["kwargs"]["template_id"], email.pk)

    def test_without_a_template_nothing_is_queued(self):
        self.template.delete()
        with patch("core.tasks.render_document_task.delay") as delay:
            response = self.client.get(self._path())

        self.assertEqual(response.status_code, 404)
        delay.assert_not_called()

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_renderer_renders_the_template_with_the_qr(self, html_to_pdf):
        pdf = render_asset_transaction_document(
            transaction_id=self.transaction.pk, template_id=self.template.pk, document_url="https://x/doc/",
        )

        self.assertEqual(pdf, b"%PDF-fake")
        self.assertIn("data:image/png;base64,", html_to_pdf.call_args.args[0])

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_status_and_download_serve_the_rendered_pdf_without_login(self, html_to_pdf):
        result = render_document_task(
            renderer=renderer_path(render_asset_transaction_document),
            kwargs={"transaction_id": self.transaction.pk, "template_id": self.template.pk, "document_url": "u"},
            filename=f"transaction_{self.transaction.pk}.pdf",
            inline=True,
        )
        with patch("core.views.document_status.AsyncResult") as core_result, \
                patch("assets.views.asset_transaction_document.AsyncResult") as asset_result:
            for mocked in (core_result, asset_result):
                mocked.return_value.state = "SUCCESS"
                mocked.return_value.result = result
            status = self.client.get(self._status()).json()
            response = self.client.get(self._download())

        self.assertEqual(status["state"], "done")
        self.assertEqual(status["download_url"], self._download())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-fake")
        self.assertIn("inline;", response["Content-Disposition"])
        self.assertFalse(default_storage.exists(result["storage_path"]))

    def test_a_task_of_another_transaction_is_not_served(self):
        other = {"status": "ok", "storage_path": "tmp/documents/x.pdf", "filename": "transaction_999999.pdf"}
        with patch("core.views.document_status.AsyncResult") as core_result, \
                patch("assets.views.asset_transaction_document.AsyncResult") as asset_result:
            for mocked in (core_result, asset_result):
                mocked.return_value.state = "SUCCESS"
                mocked.return_value.result = other
            status = self.client.get(self._status())
            response = self.client.get(self._download())

        self.assertEqual(status.status_code, 404)
        self.assertEqual(response.status_code, 403)

    def test_unknown_token_is_not_found(self):
        self.assertEqual(self.client.get(reverse("asset_transaction_document", args=["missing-token"])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("asset_transaction_document_download", args=["missing-token", "t"])).status_code,
            404,
        )
