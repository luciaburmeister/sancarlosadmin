from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from assets.models import AssetTransaction, CubaTransactionKey, TransactionTypeChoices
from assets.services.asset_transaction_email_service import AssetTransactionEmailService
from company.models import Company, WorkingZone
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


class LandfillReceiptEmailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Landfill Co")
        cls.landfill = WorkingZone.objects.create(
            company=cls.company,
            name="Vertedero Norte",
            zone_type=WorkingZone.ZoneType.VERTEDERO,
            receipt_notification_email="vertedero@example.com",
            auto_send_receipt=True,
        )
        cls.pickup = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            to_zone=None,
            data={CubaTransactionKey.LANDFILL: "Vertedero Norte"},
            date=timezone.now(),
        )
        cls.manual_landfill = WorkingZone.objects.create(
            company=cls.company,
            name="Vertedero Sur",
            zone_type=WorkingZone.ZoneType.VERTEDERO,
            receipt_notification_email="sur@example.com",
        )
        cls.manual_pickup = AssetTransaction.objects.create(
            company=cls.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            to_zone=cls.landfill,
            data={CubaTransactionKey.LANDFILL: "Vertedero Sur"},
            date=timezone.now(),
        )

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_automatic_send_queues_the_existing_email_task(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            email = self.pickup.queue_landfill_receipt_email()

        self.assertEqual(email, "vertedero@example.com")
        delay.assert_called_once_with(
            self.pickup.pk, to_email="vertedero@example.com", reply_to=None,
        )

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_flag_off_skips_automatic_send_but_manual_still_uses_the_email(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.manual_pickup.queue_landfill_receipt_email(), "")
        delay.assert_not_called()

        with self.captureOnCommitCallbacks(execute=True):
            email = self.manual_pickup.queue_landfill_receipt_email(require_auto=False)

        self.assertEqual(email, "sur@example.com")
        delay.assert_called_once_with(
            self.manual_pickup.pk, to_email="sur@example.com", reply_to=None,
        )

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_receipt_to_zone_is_not_emailed(self, delay):
        receipt = AssetTransaction.objects.create(
            company=self.company,
            transaction_type=TransactionTypeChoices.RECEIPT,
            to_zone=self.landfill,
            date=timezone.now(),
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(receipt.queue_landfill_receipt_email(), "")
        delay.assert_not_called()

    @patch("core.tasks.send_asset_transaction_email_task.delay")
    def test_named_zone_that_is_not_a_landfill_is_not_emailed(self, delay):
        WorkingZone.objects.create(
            company=self.company,
            name="Obra",
            receipt_notification_email="obra@example.com",
            auto_send_receipt=True,
        )
        pickup = AssetTransaction.objects.create(
            company=self.company,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            to_zone=self.landfill,
            data={CubaTransactionKey.LANDFILL: "Obra"},
            date=timezone.now(),
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(pickup.queue_landfill_receipt_email(), "")
        delay.assert_not_called()

    def test_automatic_send_requires_an_email(self):
        zone = WorkingZone(
            company=self.company,
            name="No email",
            zone_type=WorkingZone.ZoneType.VERTEDERO,
            auto_send_receipt=True,
        )
        with self.assertRaises(ValidationError):
            zone.clean()

    def test_receipt_email_settings_are_only_for_landfills(self):
        zone = WorkingZone(
            company=self.company,
            name="Obra",
            zone_type=WorkingZone.ZoneType.OBRA,
            receipt_notification_email="obra@example.com",
        )
        with self.assertRaises(ValidationError):
            zone.clean()
