import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.cache import cache
from django.core.mail.backends.smtp import EmailBackend as SMTPEmailBackend
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from company.models import Company, EmailEvent, EmailLog
from core.services.email_event_service import EmailEventService, SESEventPayload
from core.services.email_service import EmailService, EnvelopeEmailMessage
from general.email.providers import default_provider, get_provider, registered_providers


class EnvelopeEmailMessageTests(SimpleTestCase):
    def test_smtp_routes_only_to_envelope_recipients(self):
        message = EnvelopeEmailMessage(
            subject="Factura F-1",
            body="Documento adjunto",
            from_email="erp@polipus.test",
            to=["client@example.com"],
            cc=["employee@polipus.test"],
            envelope_recipients=["employee@polipus.test"],
        )
        backend = SMTPEmailBackend()
        backend.connection = Mock()

        self.assertTrue(backend._send(message))

        _, envelope, _ = backend.connection.sendmail.call_args.args
        self.assertEqual(envelope, ["employee@polipus.test"])
        mime = message.message()
        self.assertEqual(str(mime["To"]), "client@example.com")
        self.assertEqual(str(mime["Cc"]), "employee@polipus.test")


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="erp@polipus.test",
    SES_DELIVERY_CONFIGURATION_SET="erp-delivery",
    SES_ENGAGEMENT_CONFIGURATION_SET="erp-engagement",
)
class TrackedEmailServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Polipus")

    def test_tracked_recipients_are_individual_and_internal_copy_is_untracked(self):
        sent = EmailService.send_email(
            to=["client@example.com", "contact@example.com"],
            cc=["employee@polipus.test"],
            subject="Presupuesto #12",
            body_html="<p>Documento adjunto</p>",
            related_object=self.company,
            company=self.company,
            email_document="quote",
        )

        self.assertTrue(sent)
        self.assertEqual(len(mail.outbox), 3)
        external = mail.outbox[:2]
        visible_to = ["client@example.com", "contact@example.com"]
        self.assertTrue(all(message.to == visible_to for message in external))
        self.assertTrue(all(
            message.cc == ["employee@polipus.test"] for message in external
        ))
        self.assertEqual(
            [message.recipients() for message in external],
            [["client@example.com"], ["contact@example.com"]],
        )
        self.assertTrue(all(
            message.extra_headers["X-SES-CONFIGURATION-SET"] == "erp-engagement"
            for message in external
        ))

        audit_copy = mail.outbox[2]
        self.assertEqual(audit_copy.to, visible_to)
        self.assertEqual(audit_copy.cc, ["employee@polipus.test"])
        self.assertEqual(audit_copy.recipients(), ["employee@polipus.test"])
        self.assertNotIn("X-SES-CONFIGURATION-SET", audit_copy.extra_headers)

        for message in mail.outbox:
            mime = message.message()
            self.assertEqual(str(mime["To"]), ", ".join(visible_to))
            self.assertEqual(str(mime["Cc"]), "employee@polipus.test")

        logs = list(EmailLog.objects.order_by("to_email"))
        self.assertEqual(len(logs), 2)
        self.assertEqual(len({row.batch_id for row in logs}), 1)
        self.assertTrue(all(row.status == EmailLog.EmailStatus.SUCCESS for row in logs))
        self.assertTrue(all(
            row.delivery_status == EmailLog.DeliveryStatus.ACCEPTED for row in logs
        ))
        self.assertTrue(all(row.tracking_enabled for row in logs))

    def test_delivery_only_document_uses_delivery_configuration_set(self):
        EmailService.send_email(
            to=["client@example.com"],
            subject="Condiciones aceptadas",
            body_html="<p>Condiciones</p>",
            related_object=self.company,
            company=self.company,
            email_document="client_terms",
        )

        self.assertEqual(
            mail.outbox[0].extra_headers["X-SES-CONFIGURATION-SET"],
            "erp-delivery",
        )

    def test_recipient_is_not_sent_an_additional_cc_copy(self):
        EmailService.send_email(
            to=["employee@polipus.test"],
            cc=["EMPLOYEE@polipus.test"],
            subject="Presupuesto #12",
            body_html="<p>Documento adjunto</p>",
            related_object=self.company,
            company=self.company,
            email_document="quote",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["employee@polipus.test"])
        self.assertEqual(mail.outbox[0].cc, [])
        self.assertEqual(mail.outbox[0].recipients(), ["employee@polipus.test"])

    @override_settings(
        SES_DELIVERY_CONFIGURATION_SET="",
        SES_ENGAGEMENT_CONFIGURATION_SET="",
    )
    def test_unconfigured_tracking_preserves_the_existing_smtp_message(self):
        EmailService.send_email(
            to=["client@example.com"],
            cc=["employee@polipus.test"],
            subject="Factura F-1",
            body_html="<p>Factura</p>",
            related_object=self.company,
            company=self.company,
            email_document="sales_invoice",
        )

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["client@example.com"])
        self.assertEqual(mail.outbox[0].cc, ["employee@polipus.test"])
        self.assertEqual(
            mail.outbox[0].recipients(),
            ["client@example.com", "employee@polipus.test"],
        )
        log = EmailLog.objects.get()
        self.assertFalse(log.tracking_enabled)
        self.assertEqual(log.delivery_status, EmailLog.DeliveryStatus.UNKNOWN)


class SESEventPayloadTests(SimpleTestCase):
    """The payload reader needs no database: it only knows Amazon's JSON."""

    def test_event_type_ignores_casing_and_punctuation(self):
        self.assertEqual(
            SESEventPayload({"eventType": "DeliveryDelay"}).event_type,
            EmailEvent.EventType.DELIVERY_DELAY,
        )
        self.assertEqual(
            SESEventPayload({"notificationType": "Bounce"}).event_type,
            EmailEvent.EventType.BOUNCE,
        )
        self.assertIsNone(SESEventPayload({"eventType": "Unheard-of"}).event_type)
        self.assertIsNone(SESEventPayload({}).event_type)

    def test_bounce_details_join_classification_and_diagnostic_code(self):
        event = SESEventPayload({
            "eventType": "Bounce",
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [{
                    "emailAddress": "client@example.com",
                    "diagnosticCode": "smtp; 550 mailbox does not exist",
                }],
            },
        })

        self.assertEqual(event.recipient, "client@example.com")
        self.assertEqual(
            event.details,
            "Permanent/General — smtp; 550 mailbox does not exist",
        )

    def test_recipient_falls_back_to_a_single_destination(self):
        event = SESEventPayload({
            "eventType": "Open",
            "mail": {"destination": ["client@example.com"]},
        })
        self.assertEqual(event.recipient, "client@example.com")

        ambiguous = SESEventPayload({
            "eventType": "Open",
            "mail": {"destination": ["one@example.com", "two@example.com"]},
        })
        self.assertEqual(ambiguous.recipient, "")

    def test_section_timestamp_wins_over_the_mail_timestamp(self):
        event = SESEventPayload({
            "eventType": "Delivery",
            "mail": {"timestamp": "2026-09-17T10:00:00Z"},
            "delivery": {"timestamp": "2026-09-17T10:00:05Z"},
        })
        self.assertEqual(event.occurred_at, parse_datetime("2026-09-17T10:00:05Z"))

    def test_missing_timestamp_defaults_to_now(self):
        before = timezone.now()
        occurred_at = SESEventPayload({"eventType": "Delivery"}).occurred_at
        self.assertGreaterEqual(occurred_at, before)

    def test_email_log_id_accepts_a_bare_string_and_rejects_garbage(self):
        def log_id(value):
            return SESEventPayload(
                {"mail": {"tags": {"erp_email_log_id": value}}}
            ).email_log_id

        self.assertEqual(log_id(["42"]), 42)
        self.assertEqual(log_id("42"), 42)
        self.assertIsNone(log_id(["not-a-number"]))
        self.assertIsNone(log_id([]))
        self.assertIsNone(SESEventPayload({"mail": {}}).email_log_id)


class SESEventServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Polipus")
        self.log = EmailLog.objects.create(
            company=self.company,
            content_type=ContentType.objects.get_for_model(self.company),
            object_id=self.company.pk,
            to_email="client@example.com",
            subject="Factura F-1",
            status=EmailLog.EmailStatus.SUCCESS,
            provider="ses",
            tracking_enabled=True,
            delivery_status=EmailLog.DeliveryStatus.ACCEPTED,
        )
        self.sent_at = timezone.now()

    def payload(self, event_type, section=None):
        payload = {
            "eventType": event_type,
            "mail": {
                "messageId": "ses-message-1",
                "timestamp": self.sent_at.isoformat(),
                "destination": ["client@example.com"],
                "tags": {"erp_email_log_id": [str(self.log.pk)]},
            },
        }
        if section:
            payload.update(section)
        return payload

    def record(self, payload, provider_event_id):
        return EmailEventService.record(
            SESEventPayload(payload),
            provider="ses",
            provider_event_id=provider_event_id,
        )

    def test_delivery_and_duplicate_event_are_idempotent(self):
        delivered_at = self.sent_at + timedelta(seconds=3)
        payload = self.payload("Delivery", {
            "delivery": {
                "timestamp": delivered_at.isoformat(),
                "recipients": ["client@example.com"],
            },
        })

        first = self.record(payload, "sns-event-1")
        second = self.record(payload, "sns-event-1")

        self.assertEqual(first["status"], "processed")
        self.assertEqual(second["status"], "duplicate")
        self.log.refresh_from_db()
        self.assertEqual(self.log.delivery_status, EmailLog.DeliveryStatus.DELIVERED)
        self.assertEqual(self.log.provider_message_id, "ses-message-1")
        self.assertEqual(EmailEvent.objects.count(), 1)

    def test_open_events_record_first_last_and_count(self):
        first_at = self.sent_at + timedelta(minutes=1)
        second_at = self.sent_at + timedelta(minutes=5)
        self.record(self.payload("Open", {"open": {"timestamp": first_at.isoformat()}}), "open-1")
        self.record(self.payload("Open", {"open": {"timestamp": second_at.isoformat()}}), "open-2")

        self.log.refresh_from_db()
        self.assertEqual(self.log.open_count, 2)
        self.assertEqual(self.log.first_opened_at, first_at)
        self.assertEqual(self.log.last_opened_at, second_at)

    def test_late_delivery_does_not_overwrite_a_bounce(self):
        self.record(
            self.payload("Bounce", {
                "bounce": {
                    "timestamp": self.sent_at.isoformat(),
                    "bounceType": "Permanent",
                    "bounceSubType": "General",
                    "bouncedRecipients": [{
                        "emailAddress": "client@example.com",
                        "diagnosticCode": "mailbox does not exist",
                    }],
                },
            }),
            "bounce-1",
        )
        self.record(
            self.payload("Delivery", {
                "delivery": {
                    "timestamp": (self.sent_at + timedelta(seconds=1)).isoformat(),
                    "recipients": ["client@example.com"],
                },
            }),
            "delivery-after-bounce",
        )

        self.log.refresh_from_db()
        self.assertEqual(self.log.delivery_status, EmailLog.DeliveryStatus.BOUNCED)
        self.assertIn("mailbox does not exist", self.log.delivery_error)


@override_settings(SES_SNS_TOPIC_ARN="arn:aws:sns:eu-west-1:123456789:erp-email")
class SESEventWebhookTests(TestCase):
    url = reverse_lazy("email_event_webhook", args=["ses"])

    def setUp(self):
        cache.clear()

    @patch("core.services.email_event_service.EmailEventService.record")
    @patch("core.services.sns_signature.verify_sns_message")
    def test_verified_notification_is_forwarded_to_event_service(self, verify, record):
        record.return_value = {"status": "processed"}
        ses_payload = {"eventType": "Delivery", "mail": {"messageId": "m-1"}}
        envelope = {
            "Type": "Notification",
            "MessageId": "sns-1",
            "Message": json.dumps(ses_payload),
        }

        response = self.client.post(
            self.url, data=json.dumps(envelope), content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        verify.assert_called_once()
        record.assert_called_once()
        event, kwargs = record.call_args[0][0], record.call_args[1]
        self.assertEqual(event.event_type, EmailEvent.EventType.DELIVERY)
        self.assertEqual(event.message_id, "m-1")
        self.assertEqual(kwargs, {"provider": "ses", "provider_event_id": "sns-1"})

    @patch("core.tasks.record_email_event_task.delay")
    @patch("core.services.sns_signature.verify_sns_message")
    def test_notification_is_queued_without_touching_the_database(self, verify, delay):
        ses_payload = {"eventType": "Open", "mail": {"messageId": "m-1"}}
        envelope = {
            "Type": "Notification",
            "MessageId": "sns-1",
            "Message": json.dumps(ses_payload),
        }

        with self.assertNumQueries(0):
            response = self.client.post(
                self.url, data=json.dumps(envelope), content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "queued"})
        delay.assert_called_once_with("ses", "sns-1", ses_payload)

    @patch("core.services.sns_signature.verify_sns_message")
    def test_invalid_signature_is_rejected(self, verify):
        from core.services.sns_signature import InvalidSNSMessage

        verify.side_effect = InvalidSNSMessage("bad signature")
        response = self.client.post(
            self.url, data="{}", content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    @patch("core.services.sns_signature.verify_sns_message")
    def test_repeated_rejections_throttle_only_that_caller(self, verify):
        from core.services.sns_signature import InvalidSNSMessage
        from core.views.email_event_webhook import RejectedWebhookThrottle

        limit = RejectedWebhookThrottle().num_requests

        verify.side_effect = InvalidSNSMessage("bad signature")
        attacker = {"HTTP_X_FORWARDED_FOR": "1.2.3.4, 52.95.4.10, 10.0.0.5"}
        for _ in range(limit):
            response = self.client.post(
                self.url, data="{}", content_type="application/json", **attacker,
            )
            self.assertEqual(response.status_code, 403)

        response = self.client.post(
            self.url, data="{}", content_type="application/json", **attacker,
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(verify.call_count, limit)

        response = self.client.post(
            self.url, data="{}", content_type="application/json",
            HTTP_X_FORWARDED_FOR="34.120.5.5, 10.0.0.5",
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_provider_is_a_404(self):
        response = self.client.post(
            reverse("email_event_webhook", args=["mailgun"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)


class SNSCertificateCacheTests(SimpleTestCase):
    url = "https://sns.eu-north-1.amazonaws.com/SimpleNotificationService-abc.pem"

    def setUp(self):
        from core.services.sns_signature import _download_certificate

        cache.clear()
        _download_certificate.cache_clear()
        self.addCleanup(_download_certificate.cache_clear)

    @patch("core.services.sns_signature._HTTPS_OPENER")
    def test_certificate_is_shared_across_cold_workers(self, opener):
        from core.services.sns_signature import _download_certificate

        opener.open.return_value.__enter__.return_value.read.return_value = b"PEM"

        self.assertEqual(_download_certificate(self.url), b"PEM")
        _download_certificate.cache_clear()  # a recycled gunicorn worker
        self.assertEqual(_download_certificate(self.url), b"PEM")

        opener.open.assert_called_once()


class ClientIPTests(SimpleTestCase):
    def _ip(self, forwarded, remote="10.0.0.1"):
        from django.test import RequestFactory
        from general.client_ip import client_ip

        meta = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            meta["HTTP_X_FORWARDED_FOR"] = forwarded
        return client_ip(RequestFactory().post("/", **meta))

    def test_rightmost_public_address_wins_over_forged_ones(self):
        self.assertEqual(self._ip("8.8.8.8, 52.95.4.10, 10.0.0.5"), "52.95.4.10")

    def test_falls_back_to_remote_addr(self):
        self.assertEqual(self._ip(None), "10.0.0.1")
        self.assertEqual(self._ip("garbage, 10.0.0.5"), "10.0.0.1")


class EmailProviderRegistryTests(SimpleTestCase):
    def test_ses_is_registered_under_its_key(self):
        provider = get_provider("ses")
        self.assertIsNotNone(provider)
        self.assertEqual(provider.key, "ses")
        self.assertIn(provider, registered_providers())

    def test_unknown_provider_is_none(self):
        self.assertIsNone(get_provider("carrier-pigeon"))

    @override_settings(EMAIL_TRACKING_PROVIDER="ses")
    def test_default_provider_follows_the_setting(self):
        self.assertEqual(default_provider().key, "ses")

    @override_settings(EMAIL_TRACKING_PROVIDER="nope")
    def test_default_provider_is_none_when_unregistered(self):
        self.assertIsNone(default_provider())

    @override_settings(
        SES_DELIVERY_CONFIGURATION_SET="",
        SES_ENGAGEMENT_CONFIGURATION_SET="",
    )
    def test_ses_reports_tracking_disabled_without_configuration_sets(self):
        provider = get_provider("ses")
        self.assertFalse(provider.tracking_enabled({"delivery": True, "opens": True}))
        self.assertEqual(
            provider.outgoing_headers(
                log=None, batch_id=None, policy={"delivery": True, "opens": True}
            ),
            {},
        )

    @override_settings(
        SES_DELIVERY_CONFIGURATION_SET="erp-delivery",
        SES_ENGAGEMENT_CONFIGURATION_SET="erp-engagement",
    )
    def test_ses_headers_carry_the_log_id_and_the_engagement_set(self):
        provider = get_provider("ses")
        batch_id = uuid.uuid4()
        headers = provider.outgoing_headers(
            log=SimpleNamespace(pk=7),
            batch_id=batch_id,
            policy={"delivery": True, "opens": True},
        )

        self.assertEqual(headers["X-SES-CONFIGURATION-SET"], "erp-engagement")
        self.assertEqual(
            headers["X-SES-MESSAGE-TAGS"],
            f"erp_email_log_id=7,erp_email_batch={batch_id.hex}",
        )
