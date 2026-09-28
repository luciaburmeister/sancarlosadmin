from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.models import AssetTransaction, TransactionTypeChoices
from company.models import Company
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
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
            html="<p>{{ asset_name }}</p><img src=\"{{ document_qr }}\">",
        )

    def test_new_transaction_gets_a_public_document_url(self):
        self.assertTrue(self.transaction.public_token)
        self.assertEqual(
            self.transaction.document_url,
            "https://erp.example.com"
            + reverse("asset_transaction_document", args=[self.transaction.public_token]),
        )

    def test_export_context_qr_encodes_the_document_url(self):
        context = AssetTransactionExportStrategy(self.transaction).get_context()

        self.assertEqual(context["document_url"], self.transaction.document_url)
        self.assertTrue(context["document_qr"].startswith("data:image/png;base64,"))

    def test_public_document_renders_the_export_template(self):
        response = self.client.get(self.transaction.document_url.replace("https://erp.example.com", ""))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("data:image/png;base64,", body)

    def test_unknown_token_is_not_found(self):
        response = self.client.get(reverse("asset_transaction_document", args=["missing-token"]))

        self.assertEqual(response.status_code, 404)
