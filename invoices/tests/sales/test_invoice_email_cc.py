"""
The person who finalizes an invoice is copied on the email the client gets.

Replies already come back to them (`sender_reply_to`); the Cc gives them the
copy of what actually went out, attachment included.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from company.models import Company
from general.email import sender_cc
from invoices.models import SalesInvoice
from invoices.services.invoice_email_service import InvoiceEmailService
from invoices.services.sales_invoice_service import SalesInvoiceService
from sales.models import Client


class SenderCcTest(TestCase):
    def test_sender_cc_is_empty_without_a_usable_user(self):
        self.assertEqual(sender_cc(None), [])
        self.assertEqual(sender_cc(User(username="nobody", email="")), [])
        self.assertEqual(
            sender_cc(User(username="ana", email="ana@acme.test")),
            ["ana@acme.test"],
        )


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class FinalizeInvoiceCcTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.client_obj = Client.objects.create(
            name="Cliente", company=self.company, email="cliente@example.com"
        )
        self.invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj
        )

    def _finalize(self, **kwargs):
        # The email is queued once the finalize commits
        with patch("core.tasks.send_invoice_email_task.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            SalesInvoiceService.finalize_invoice(self.invoice, **kwargs)
        return delay

    def test_finalizing_user_is_copied_on_the_client_email(self):
        delay = self._finalize(
            reply_to=["ana@acme.test"], cc_emails=["ana@acme.test"]
        )

        delay.assert_called_once()
        kwargs = delay.call_args.kwargs
        self.assertEqual(kwargs["to_email"], "cliente@example.com")
        self.assertEqual(kwargs["reply_to"], ["ana@acme.test"])
        self.assertEqual(kwargs["cc_emails"], ["ana@acme.test"])

    def test_no_cc_when_nobody_triggered_the_finalize(self):
        delay = self._finalize()

        self.assertEqual(delay.call_args.kwargs["cc_emails"], [])

    def test_the_sender_is_not_copied_on_their_own_message(self):
        """A user finalizing an invoice for their own address gets it once."""
        delay = self._finalize(cc_emails=["CLIENTE@example.com"])

        self.assertEqual(delay.call_args.kwargs["cc_emails"], [])


class InvoiceEmailCcTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.client_obj = Client.objects.create(
            name="Cliente", company=self.company, email="cliente@example.com"
        )
        self.invoice = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj, invoice_number="INV-1"
        )

    def _send(self, **kwargs):
        with patch(
            "invoices.services.invoice_email_service.PDFService.render_object_pdf",
            return_value=b"%PDF-1.4",
        ):
            sent = InvoiceEmailService.send_invoice_email(self.invoice, **kwargs)
        self.assertTrue(sent)
        return mail.outbox[-1]

    def test_cc_lands_on_the_message(self):
        message = self._send(cc_emails=["ana@acme.test"])

        self.assertEqual(message.to, ["cliente@example.com"])
        self.assertEqual(message.cc, ["ana@acme.test"])
        self.assertIn("ana@acme.test", message.recipients())

    def test_message_without_cc_is_unchanged(self):
        message = self._send()

        self.assertEqual(message.cc, [])
