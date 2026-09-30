"""
EInvoicingService.cancel_invoice, around the provider call:

- a failure before the request is sent (e.g. an API key that no longer
  decrypts) turns the pending cancellation into an error, so the advance
  stays deductible;
- without an active e-invoicing account an invoice already registered at
  the AEAT is refused, not cancelled locally (the task and the admin);
  one that was never registered still is.
"""
from decimal import Decimal
from unittest import mock

from cryptography.fernet import InvalidToken
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from apps.einvoicing.services import EInvoicingService
from apps.einvoicing.tasks import cancel_invoice_at_aeat
from invoices.models import SalesInvoiceStatus
from invoices.services.advances import AdvanceDeductionService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class CancelBeforeSendingTests(AdvanceExpedienteMixin, TestCase):
    """A final advance of 300.00 whose company's API key no longer decrypts."""

    def setUp(self):
        super().setUp()
        self.advance, _line = self.make_advance(Decimal("300.00"), "30")
        EInvoicingAccount.objects.create(
            company=self.company, name="Verifacti", api_key_encrypted="not-a-fernet-token",
        )

    def test_the_marker_becomes_an_error_and_the_advance_stays_deductible(self):
        with mock.patch("apps.einvoicing.providers.verifacti.requests.post") as post:
            with self.assertRaises(InvalidToken):
                cancel_invoice_at_aeat(self.advance.pk)

        post.assert_not_called()
        marker = EInvoicingSubmission.objects.get(
            sales_invoice=self.advance, action=SubmissionAction.CANCEL,
        )
        self.assertEqual(marker.status, SubmissionStatus.ERROR)
        self.assertTrue(marker.error_message)
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.FINAL)
        self.assertTrue(self.summary()["advances"][0]["deductible"])
        # The closing invoice still deducts it
        invoice = self.invoice_goods()
        self.assertEqual(len(AdvanceDeductionService.apply(invoice)), 1)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class CancelWithoutAnAccountTests(AdvanceExpedienteMixin, TestCase):
    """A final advance of 300.00 (IVA 21 %: 363.00); the company's only
    e-invoicing account has been switched off."""

    DETAIL = "admin:invoices_salesinvoice_cancel_aeat_detail_action"

    def setUp(self):
        super().setUp()
        self.advance, _line = self.make_advance(Decimal("300.00"), "30")
        self.account = EInvoicingAccount.objects.create(
            company=self.company, name="Verifacti", is_active=False,
        )

    def sent(self, status):
        EInvoicingSubmission.objects.create(
            account=self.account, sales_invoice=self.advance,
            action=SubmissionAction.CREATE, status=status,
        )

    def assert_still_final(self):
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.FINAL)
        self.assertFalse(
            EInvoicingSubmission.objects.filter(action=SubmissionAction.CANCEL).exists()
        )
        # Still pending deduction: nothing was released
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("300.00"))

    def test_an_invoice_registered_at_the_aeat_is_refused(self):
        for status in (SubmissionStatus.ACCEPTED, SubmissionStatus.SUBMITTED, SubmissionStatus.PENDING):
            with self.subTest(status=status):
                EInvoicingSubmission.objects.all().delete()
                self.sent(status)

                with self.assertRaises(ValidationError):
                    EInvoicingService.cancel_invoice(self.advance)
                self.assert_still_final()

    def test_the_task_reports_the_refusal(self):
        self.sent(SubmissionStatus.ACCEPTED)

        result = cancel_invoice_at_aeat(self.advance.pk)

        self.assertEqual(result["status"], "refused")
        self.assert_still_final()

    def test_the_admin_refuses_it(self):
        self.sent(SubmissionStatus.ACCEPTED)
        self.client.force_login(User.objects.create_superuser("canceller", "c@x.com", "pw"))

        with mock.patch("invoices.admin.cancel_invoice_at_aeat") as task:
            # The action's dialog, submitted (htmx posts it)
            response = self.client.post(reverse(self.DETAIL, args=[self.advance.pk]), {"_form_submitted": "True"})

        task.delay.assert_not_called()
        self.assertEqual([m.level for m in get_messages(response.wsgi_request)], [messages.ERROR])
        self.assert_still_final()

    def test_one_the_aeat_rejected_is_cancelled_locally(self):
        self.sent(SubmissionStatus.REJECTED)

        result = cancel_invoice_at_aeat(self.advance.pk)

        self.assertEqual(result["status"], "cancelled")
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.CANCELLED)
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))
