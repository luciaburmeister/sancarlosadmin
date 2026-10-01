from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from assets.models import AssetTransaction, TransactionTypeChoices
from assets.services.asset_transaction_email_service import AssetTransactionEmailService
from company.models import Company
from configurator.models import ConfigurationExportTemplate, ExportTemplateKind, ExportTemplateSubtype


class AssetTransactionEmailTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )
        ConfigurationExportTemplate.objects.create(
            company=cls.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            subtype=ExportTemplateSubtype.DOCUMENT,
            name="QR",
            html="<p>SCAN-DOC</p>",
        )
        ConfigurationExportTemplate.objects.create(
            company=cls.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            subtype=ExportTemplateSubtype.EMAIL,
            name="Email",
            html="<p>EMAIL-DOC</p>",
        )

    @patch("assets.services.asset_transaction_email_service.EmailService.send_email", return_value=True)
    @patch("core.services.pdf_service.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_email_uses_the_email_subtype_of_the_same_kind(self, html_to_pdf, send_email):
        sent = AssetTransactionEmailService.send_transaction_email(
            self.transaction, to_email="client@example.com",
        )

        self.assertTrue(sent)
        rendered_html = html_to_pdf.call_args.args[0]
        self.assertIn("EMAIL-DOC", rendered_html)
        self.assertNotIn("SCAN-DOC", rendered_html)
        send_email.assert_called_once()


class AssetTransactionEmailFallbackTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Legacy Co")
        cls.transaction = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
        )
        ConfigurationExportTemplate.objects.create(
            company=cls.company,
            kind=ExportTemplateKind.ASSET_TRANSACTION,
            name="Albarán",
            html="<p>LEGACY-DOC</p>",
        )

    @patch("assets.services.asset_transaction_email_service.EmailService.send_email", return_value=True)
    @patch("core.services.pdf_service.PDFService.html_to_pdf", return_value=b"%PDF-fake")
    def test_email_keeps_using_the_existing_template(self, html_to_pdf, send_email):
        sent = AssetTransactionEmailService.send_transaction_email(
            self.transaction, to_email="client@example.com",
        )

        self.assertTrue(sent)
        self.assertIn("LEGACY-DOC", html_to_pdf.call_args.args[0])
        send_email.assert_called_once()
