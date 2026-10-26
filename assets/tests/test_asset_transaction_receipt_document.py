"""
assets/tests/test_asset_transaction_receipt_document.py

Covers the per-DI document (assets.documents.generate_receipt_document and
the public asset_transaction_receipt_document view) -- same template and
pipeline as AssetTransaction's own document (see
test_asset_transaction_document.py), but rendered once per DI part, using
that receipt's own nro_albaran/material_vertido/peso_neto instead of the
transaction's.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.documents import generate_receipt_document
from assets.models import AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices
from company.models import Company
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind


@override_settings(BACKEND_URL="https://erp.example.com")
class AssetTransactionReceiptDocumentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
            data={"material_vertido": "Mixto", "nro_albaran": "ORIGINAL-1"},
        )
        cls.template = ConfigurationExportTemplate.objects.create(
            company=cls.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            name="DI",
            html="<p>DI {{ data.nro_albaran }} - {{ data.material_vertido }} - {{ data.part_number }}/{{ data.total_parts }}</p>",
        )

    def setUp(self):
        cache.clear()
        self.receipt = AssetTransactionReceipt.objects.create(
            transaction=self.transaction, nro_albaran="PART-1",
            data={"material_vertido": "Madera"},
        )

    def _path(self):
        return reverse("asset_transaction_receipt_document", args=[self.receipt.public_token])

    def test_receipt_gets_its_own_public_document_url(self):
        self.assertTrue(self.receipt.public_token)
        self.assertEqual(self.receipt.document_url, "https://erp.example.com" + self._path())
        self.assertNotEqual(self.receipt.document_url, self.transaction.document_url)

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_renders_with_the_receipts_own_data_not_the_transactions(self, html_to_pdf):
        self.assertTrue(generate_receipt_document(self.receipt.pk))

        rendered_html = html_to_pdf.call_args.args[0]
        # The template body uses the receipt's own data, not the transaction's
        # (a raw copy of the transaction's `data` still appears in the
        # debug-only context_json dump, which is fine -- it's never shown).
        self.assertIn("<body>\n  DI PART-1 - Madera", rendered_html)
        self.receipt.refresh_from_db()
        self.assertIsNotNone(self.receipt.document_generated_at)

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_a_sibling_part_renders_with_its_own_data(self, html_to_pdf):
        sibling = self.receipt.split_into_parts(2)[0]
        sibling.nro_albaran = "PART-2"
        sibling.data = {"material_vertido": "Cartón"}
        sibling.save()

        generate_receipt_document(sibling.pk)

        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn("PART-2", rendered_html)
        self.assertIn("Cartón", rendered_html)

    @patch("assets.documents.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_renders_its_own_part_number_and_total_parts(self, html_to_pdf):
        siblings = self.receipt.split_into_parts(2)

        generate_receipt_document(siblings[0].pk)

        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn("2/2", rendered_html)

    def test_first_visit_queues_the_render_and_answers_with_the_status_page(self):
        with patch("assets.tasks.generate_asset_transaction_receipt_document.delay") as delay:
            delay.return_value.id = "task-1"
            response = self.client.get(self._path())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{self._path()}"')
        delay.assert_called_once_with(self.receipt.pk)

    def test_a_stored_document_is_served_without_rendering(self):
        self.receipt.document_file.save("receipt.pdf", ContentFile(b"%PDF-stored"))
        with patch("assets.tasks.generate_asset_transaction_receipt_document.delay") as delay:
            response = self.client.get(self._path())

        delay.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-stored")

    def test_unknown_token_is_not_found(self):
        response = self.client.get(reverse("asset_transaction_receipt_document", args=["missing-token"]))
        self.assertEqual(response.status_code, 404)


class AssetTransactionReceiptDocumentAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.user = get_user_model().objects.create_superuser(username="doc_admin2", email="b@b.com", password="x")
        cls.company = Company.objects.create(name="Taller Admin Co 2")
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company, transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(),
        )
        cls.receipt = AssetTransactionReceipt.objects.create(transaction=cls.transaction, nro_albaran="A-1")

    def setUp(self):
        self.client.force_login(self.user)

    def test_regenerate_queues_a_forced_render(self):
        ConfigurationExportTemplate.objects.create(
            company=self.company, kind=ExportTemplateKind.ASSET_TRANSACTION, name="DI", html="<p>x</p>",
        )
        url = reverse("admin:assets_assettransactionreceipt_regenerate_document", args=[self.receipt.pk])

        with patch("assets.tasks.generate_asset_transaction_receipt_document.delay") as delay:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        delay.assert_called_once_with(self.receipt.pk, force=True)

    def test_the_button_sits_on_the_receipt_change_page(self):
        response = self.client.get(reverse("admin:assets_assettransactionreceipt_change", args=[self.receipt.pk]))

        self.assertContains(
            response, reverse("admin:assets_assettransactionreceipt_regenerate_document", args=[self.receipt.pk]),
        )

    def test_transaction_page_links_to_each_parts_document(self):
        response = self.client.get(reverse("admin:assets_assettransaction_change", args=[self.transaction.pk]))

        self.assertContains(response, reverse("asset_transaction_receipt_document", args=[self.receipt.public_token]))
