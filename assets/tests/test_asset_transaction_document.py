from unittest.mock import patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.documents import generate_document
from assets.models import AssetTransaction, TransactionTypeChoices
from company.models import Company
from configurator.models import (
    ConfigurationExportTemplate,
    ExportTemplateKind,
    ExportTemplateSubtype,
)
from configurator.services.export.asset_transaction import AssetTransactionExportStrategy


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
            html="<p>SCAN-DOC {{ asset_name }}</p><img src=\"{{ document_qr }}\">",
        )

    def setUp(self):
        cache.clear()

    def _path(self):
        return reverse("asset_transaction_document", args=[self.transaction.public_token])

    def _status(self, task_id="task-1"):
        return reverse("asset_transaction_document_status", args=[self.transaction.public_token]) + f"?task_id={task_id}"

    def _get_queued(self):
        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay:
            delay.return_value.id = "task-1"
            response = self.client.get(self._path())
        return response, delay

    def _store(self, content=b"%PDF-stored"):
        self.transaction.document_file.save("transaction.pdf", ContentFile(content))

    def test_new_transaction_gets_a_public_document_url(self):
        self.assertTrue(self.transaction.public_token)
        self.assertEqual(self.transaction.document_url, "https://erp.example.com" + self._path())

    def test_export_context_qr_encodes_the_document_url(self):
        context = AssetTransactionExportStrategy(self.transaction).get_context()

        self.assertEqual(context["document_url"], self.transaction.document_url)
        self.assertTrue(context["document_qr"].startswith("data:image/png;base64,"))

    def test_first_visit_queues_the_render_and_answers_with_the_status_page(self):
        response, delay = self._get_queued()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self._path()}"')
        delay.assert_called_once_with(self.transaction.pk)

    def test_visits_while_rendering_share_the_queued_task(self):
        self._get_queued()
        with patch("assets.documents.AsyncResult") as result:
            result.return_value.failed.return_value = False
            _, delay = self._get_queued()

        delay.assert_not_called()

    def test_a_stored_document_is_served_without_rendering(self):
        self._store()
        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay:
            response = self.client.get(self._path())

        delay.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("inline;", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-stored")

    def test_status_is_done_once_the_document_is_stored(self):
        with patch("assets.views.asset_transaction_document.AsyncResult") as result:
            result.return_value.failed.return_value = False
            result.return_value.successful.return_value = False
            self.assertEqual(self.client.get(self._status()).json()["state"], "running")
            self._store()
            self.assertEqual(self.client.get(self._status()).json()["state"], "done")

    def test_status_reports_a_failed_render(self):
        with patch("assets.views.asset_transaction_document.AsyncResult") as result:
            result.return_value.failed.return_value = True
            self.assertEqual(self.client.get(self._status()).json()["state"], "error")

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_generate_stores_the_document_template_with_the_qr(self, html_to_pdf):
        ConfigurationExportTemplate.objects.create(
            company=self.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            subtype=ExportTemplateSubtype.EMAIL,
            name="Email",
            html="<p>EMAIL-DOC</p>",
        )

        self.assertTrue(generate_document(self.transaction.pk))

        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn("SCAN-DOC", rendered_html)
        self.assertNotIn("EMAIL-DOC", rendered_html)
        self.assertIn("data:image/png;base64,", rendered_html)
        self.transaction.refresh_from_db()
        self.assertIsNotNone(self.transaction.document_generated_at)
        with self.transaction.document_file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-fake")

    def _generate(self, content):
        with patch("assets.documents.PDFService.html_to_pdf", return_value=content) as html_to_pdf:
            self.assertTrue(generate_document(self.transaction.pk))
        self.transaction.refresh_from_db()
        return html_to_pdf

    def _stored_content(self):
        with self.transaction.document_file.open("rb") as fh:
            return fh.read()

    def test_an_unchanged_document_is_not_rendered_again(self):
        self._generate(b"%PDF-1")

        html_to_pdf = self._generate(b"%PDF-2")

        html_to_pdf.assert_not_called()
        self.assertEqual(self._stored_content(), b"%PDF-1")

    def test_saving_the_transaction_re_renders_the_document(self):
        self._generate(b"%PDF-1")

        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.transaction.notes = "Corrected"
            self.transaction.save(update_fields=["notes"])
        delay.assert_called_once_with(self.transaction.pk)

        self._generate(b"%PDF-2")
        self.assertEqual(self._stored_content(), b"%PDF-2")

    def test_billing_status_changes_do_not_re_render(self):
        self._generate(b"%PDF-1")

        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.transaction.save(update_fields=["billing_status"])

        delay.assert_not_called()
        # Still current: the admin must not show it as waiting for a re-render.
        self.transaction.refresh_from_db()
        self.assertGreaterEqual(self.transaction.document_generated_at, self.transaction.updated_at)

    def test_saving_without_a_stored_document_queues_nothing(self):
        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            self.transaction.notes = "Edited"
            self.transaction.save()

        delay.assert_not_called()

    def test_a_render_of_an_older_version_is_discarded(self):
        def edit_while_rendering(html):
            AssetTransaction.objects.filter(pk=self.transaction.pk).update(updated_at=timezone.now())
            return b"%PDF-stale"

        with patch("assets.documents.PDFService.html_to_pdf", side_effect=edit_while_rendering):
            generate_document(self.transaction.pk)

        self.transaction.refresh_from_db()
        self.assertFalse(self.transaction.document_file)

    def test_without_a_template_nothing_is_queued(self):
        self.template.delete()
        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay:
            response = self.client.get(self._path())

        self.assertEqual(response.status_code, 404)
        delay.assert_not_called()
        self.assertFalse(generate_document(self.transaction.pk))

    def test_unknown_token_is_not_found(self):
        self.assertEqual(self.client.get(reverse("asset_transaction_document", args=["missing-token"])).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("asset_transaction_document_status", args=["missing-token"])).status_code, 404,
        )


class AssetTransactionDocumentAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.user = get_user_model().objects.create_superuser(username="doc_admin", email="a@a.com", password="x")
        cls.company = Company.objects.create(name="Taller Admin Co")
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company, transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _regenerate(self):
        url = reverse("admin:assets_assettransaction_regenerate_document", args=[self.transaction.pk])
        with patch("assets.tasks.generate_asset_transaction_document.delay") as delay:
            response = self.client.get(url)
        return response, delay

    def test_regenerate_queues_a_forced_render(self):
        ConfigurationExportTemplate.objects.create(
            company=self.company, kind=ExportTemplateKind.ASSET_TRANSACTION, name="Albarán", html="<p>x</p>",
        )

        response, delay = self._regenerate()

        self.assertEqual(response.status_code, 302)
        delay.assert_called_once_with(self.transaction.pk, force=True)

    def test_the_button_sits_next_to_view_document(self):
        response = self.client.get(reverse("admin:assets_assettransaction_change", args=[self.transaction.pk]))

        self.assertContains(response, reverse("admin:assets_assettransaction_regenerate_document", args=[self.transaction.pk]))

    def test_regenerate_needs_change_permission(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission

        viewer = get_user_model().objects.create_user(username="doc_viewer", password="x", is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_assettransaction"))
        self.client.force_login(viewer)

        response, delay = self._regenerate()

        self.assertEqual(response.status_code, 403)
        delay.assert_not_called()

    def test_regenerate_without_a_template_queues_nothing(self):
        response, delay = self._regenerate()

        self.assertEqual(response.status_code, 302)
        delay.assert_not_called()

    @patch("assets.documents.PDFService.html_to_pdf")
    def test_forced_render_replaces_a_current_document(self, html_to_pdf):
        ConfigurationExportTemplate.objects.create(
            company=self.company, kind=ExportTemplateKind.ASSET_TRANSACTION, name="Albarán", html="<p>x</p>",
        )
        html_to_pdf.return_value = b"%PDF-1"
        generate_document(self.transaction.pk)
        html_to_pdf.return_value = b"%PDF-2"

        generate_document(self.transaction.pk, force=True)

        self.transaction.refresh_from_db()
        with self.transaction.document_file.open("rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-2")
