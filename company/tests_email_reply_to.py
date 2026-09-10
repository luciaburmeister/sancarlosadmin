from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.db import DatabaseError
from django.test import TestCase

from company.models import Company, EmailReplyTo
from core.services.email_service import EmailService
from general.email import document_choices, resolve_reply_to, sender_reply_to


class EmailReplyToResolutionTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def test_no_configuration_leaves_reply_to_empty(self):
        self.assertEqual(
            resolve_reply_to(company=self.company, email_document="sales_invoice"), []
        )

    def test_generic_company_reply_to_applies_to_every_document(self):
        self.company.reply_to_email = "hola@acme.test"
        self.company.save(update_fields=["reply_to_email"])

        self.assertEqual(
            resolve_reply_to(company=self.company, email_document="sales_invoice"),
            ["hola@acme.test"],
        )
        self.assertEqual(
            resolve_reply_to(company=self.company, email_document="quote"),
            ["hola@acme.test"],
        )

    def test_document_type_overrides_the_generic_address(self):
        self.company.reply_to_email = "hola@acme.test"
        self.company.save(update_fields=["reply_to_email"])
        EmailReplyTo.objects.create(
            company=self.company,
            email_document="sales_invoice",
            reply_to_email="facturacion@acme.test",
        )

        self.assertEqual(
            resolve_reply_to(company=self.company, email_document="sales_invoice"),
            ["facturacion@acme.test"],
        )
        # Anything without its own row still falls back to the generic one.
        self.assertEqual(
            resolve_reply_to(company=self.company, email_document="quote"),
            ["hola@acme.test"],
        )

    def test_sending_user_wins_over_both(self):
        self.company.reply_to_email = "hola@acme.test"
        self.company.save(update_fields=["reply_to_email"])
        EmailReplyTo.objects.create(
            company=self.company,
            email_document="sales_invoice",
            reply_to_email="facturacion@acme.test",
        )

        self.assertEqual(
            resolve_reply_to(
                company=self.company,
                email_document="sales_invoice",
                sender_email="ana@acme.test",
            ),
            ["ana@acme.test"],
        )

    def test_sender_reply_to_is_empty_without_a_usable_user(self):
        self.assertEqual(sender_reply_to(None), [])
        self.assertEqual(
            sender_reply_to(User(username="nobody", email="")), []
        )
        self.assertEqual(
            sender_reply_to(User(username="ana", email="ana@acme.test")),
            ["ana@acme.test"],
        )

    def test_registry_exposes_every_document_type_uniquely(self):
        choices = document_choices()
        keys = [key for key, _label in choices]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("sales_invoice", keys)
        self.assertIn("provider_quote", keys)


class EmailServiceReplyToTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Acme", reply_to_email="hola@acme.test"
        )
        EmailReplyTo.objects.create(
            company=self.company,
            email_document="sales_invoice",
            reply_to_email="facturacion@acme.test",
        )

    def _send(self, **kwargs):
        EmailService.send_email(
            to=["client@example.com"],
            subject="Hola",
            body_html="<p>Hola</p>",
            company=self.company,
            **kwargs,
        )
        return mail.outbox[-1]

    def test_send_email_stamps_the_configured_document_reply_to(self):
        message = self._send(email_document="sales_invoice")
        self.assertEqual(message.reply_to, ["facturacion@acme.test"])

    def test_send_email_falls_back_to_the_generic_reply_to(self):
        message = self._send(email_document="quote")
        self.assertEqual(message.reply_to, ["hola@acme.test"])

    def test_explicit_reply_to_wins(self):
        message = self._send(
            email_document="sales_invoice", reply_to=["ana@acme.test"]
        )
        self.assertEqual(message.reply_to, ["ana@acme.test"])

    def test_untagged_send_still_gets_the_generic_reply_to(self):
        message = self._send()
        self.assertEqual(message.reply_to, ["hola@acme.test"])

    def test_a_broken_lookup_never_stops_the_email(self):
        """
        Resolution runs a query on every send — before the migration lands, or
        if the table is otherwise unreachable, the message must still go out
        with no Reply-To rather than fail.
        """
        with patch(
            "company.models.EmailReplyTo.objects.filter",
            side_effect=DatabaseError("relation does not exist"),
        ):
            with self.assertLogs("general.email.reply_to", level="ERROR"):
                message = self._send(email_document="sales_invoice")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(message.to, ["client@example.com"])
        self.assertEqual(message.reply_to, [])
