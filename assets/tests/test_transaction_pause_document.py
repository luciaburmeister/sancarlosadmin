"""
assets/tests/test_transaction_pause_document.py

Covers the per-pause document (assets.documents.generate_pause_document and
the public asset_transaction_pause_document view) -- same template and
pipeline as AssetTransaction's own document, but rendered for the movement's
second DI, with is_pause/original_di_number/original_pickup_date laid on top
so the template can note it doesn't start from the documented origin.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.documents import generate_pause_document
from assets.models import AssetTransaction, AssetTransactionPause, TransactionTypeChoices
from company.models import Company
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind

User = get_user_model()


@override_settings(BACKEND_URL="https://erp.example.com")
class TransactionPauseDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.movement = AssetTransaction.objects.create(
            company=cls.company, transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"cubaEstado": "Cuba llena"},
        )
        cls.template = ConfigurationExportTemplate.objects.create(
            company=cls.company, kind=ExportTemplateKind.ASSET_TRANSACTION, name="DI",
            html=(
                "<p>Nº {{ document_number }} - Fecha {{ date|date:'Y-m-d' }} - "
                "{% if data.is_pause %}Continúa desde {{ data.original_di_number }}{% endif %}</p>"
            ),
        )

    def setUp(self):
        self.pause = AssetTransactionPause.objects.create(movement=self.movement)

    def _path(self):
        return reverse("asset_transaction_pause_document", args=[self.pause.public_token])

    def test_pause_gets_its_own_public_document_url(self):
        self.assertTrue(self.pause.public_token)
        self.assertEqual(
            self.pause.document_url, "https://erp.example.com" + self._path(),
        )
        self.assertNotEqual(self.pause.document_url, self.movement.document_url)

    def test_pause_gets_its_own_di_number_distinct_from_the_movement(self):
        self.assertIsNotNone(self.pause.di_number)
        self.assertNotEqual(self.pause.di_number, self.movement.di_number)

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_renders_with_the_pause_marker_and_original_di_number(self, html_to_pdf):
        self.assertTrue(generate_pause_document(self.pause.pk))

        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn(f"Nº {self.pause.di_number}", rendered_html)
        self.assertIn(f"Continúa desde {self.movement.di_number}", rendered_html)
        self.pause.refresh_from_db()
        self.assertIsNotNone(self.pause.document_generated_at)

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_prints_the_pauses_own_date_by_default(self, html_to_pdf):
        generate_pause_document(self.pause.pk)

        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn(f"Fecha {timezone.localtime(self.movement.date).strftime('%Y-%m-%d')}", rendered_html)

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_a_hand_edited_date_overrides_the_movements_date_on_the_document(self, html_to_pdf):
        corrected = timezone.make_aware(timezone.datetime(2026, 10, 20, 9, 0))
        self.pause.date = corrected
        self.pause.save()

        generate_pause_document(self.pause.pk, force=True)

        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn("Fecha 2026-10-20", rendered_html)
        self.assertNotIn(f"Fecha {timezone.localtime(self.movement.date).strftime('%Y-%m-%d')}", rendered_html)

    def test_first_visit_queues_the_render_and_answers_with_the_status_page(self):
        with patch("assets.tasks.generate_asset_transaction_pause_document.delay") as delay:
            delay.return_value.id = "task-1"
            response = self.client.get(self._path())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self._path()}"')
        delay.assert_called_once_with(self.pause.pk)

    def test_unknown_token_is_not_found(self):
        response = self.client.get(
            reverse("asset_transaction_pause_document", args=["missing-token"])
        )
        self.assertEqual(response.status_code, 404)


class TransactionPauseDocumentAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(username="pause_admin", email="a@a.com", password="x")
        cls.company = Company.objects.create(name="Taller Admin Co")
        cls.movement = AssetTransaction.objects.create(
            company=cls.company, transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"cubaEstado": "Cuba llena"},
        )
        cls.pause = AssetTransactionPause.objects.create(movement=cls.movement)

    def setUp(self):
        self.client.force_login(self.user)

    def test_regenerate_queues_a_forced_render(self):
        ConfigurationExportTemplate.objects.create(
            company=self.company, kind=ExportTemplateKind.ASSET_TRANSACTION, name="DI", html="<p>x</p>",
        )
        url = reverse(
            "admin:assets_assettransactionpause_regenerate_document", args=[self.pause.pk],
        )

        with patch("assets.tasks.generate_asset_transaction_pause_document.delay") as delay:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        delay.assert_called_once_with(self.pause.pk, force=True)

    def test_shown_as_a_read_only_inline_on_the_movement_change_page(self):
        url = reverse("admin:assets_assettransaction_change", args=[self.movement.pk])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.pause.di_number))
