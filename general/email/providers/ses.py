"""Amazon SES, receiving its event notifications through Amazon SNS."""

import json
import logging

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from .base import EmailProvider, InvalidWebhook, register

logger = logging.getLogger(__name__)


@register
class SESProvider(EmailProvider):
    key = "ses"
    label = _("Amazon SES")

    # --- outgoing -----------------------------------------------------------

    def _configuration_set(self, policy):
        """
        SES only reports on messages sent through a configuration set, so the
        set we pick is also what decides whether this email is tracked at all.
        An empty value means "not configured": the send falls back to SMTP.
        """
        if policy["opens"]:
            return (
                getattr(settings, "SES_ENGAGEMENT_CONFIGURATION_SET", "")
                or getattr(settings, "SES_DELIVERY_CONFIGURATION_SET", "")
            )
        if policy["delivery"]:
            return getattr(settings, "SES_DELIVERY_CONFIGURATION_SET", "")
        return ""

    def tracking_enabled(self, policy):
        return bool(self._configuration_set(policy))

    def outgoing_headers(self, *, log, batch_id, policy):
        configuration_set = self._configuration_set(policy)
        if not configuration_set:
            return {}
        # The log id tag is what lets an event attribute back to one exact row
        # instead of being matched on the provider's message id.
        return {
            "X-SES-CONFIGURATION-SET": configuration_set,
            "X-SES-MESSAGE-TAGS": (
                f"erp_email_log_id={log.pk},erp_email_batch={batch_id.hex}"
            ),
        }

    # --- incoming -----------------------------------------------------------

    def handle_webhook(self, request):
        from core.services.email_event_service import EmailEventService
        from core.services.sns_signature import (
            InvalidSNSMessage,
            confirm_sns_subscription,
            verify_sns_message,
        )

        expected_topic = getattr(settings, "SES_SNS_TOPIC_ARN", "")
        if not expected_topic:
            return {"error": "SES events are not configured"}, 503

        try:
            envelope = json.loads(request.body)
            verify_sns_message(envelope, expected_topic_arn=expected_topic)
        except (json.JSONDecodeError, UnicodeDecodeError, InvalidSNSMessage) as exc:
            raise InvalidWebhook("Invalid SNS notification") from exc

        message_type = envelope.get("Type")
        if message_type == "SubscriptionConfirmation":
            try:
                confirm_sns_subscription(envelope)
            except Exception:
                logger.exception("Could not confirm Amazon SNS email-event subscription")
                return {"error": "Subscription confirmation failed"}, 502
            return {"status": "subscription confirmed"}, 200

        if message_type != "Notification":
            return {"status": "ignored"}, 200

        try:
            payload = json.loads(envelope["Message"])
        except (KeyError, TypeError, json.JSONDecodeError):
            return {"error": "Invalid SES event"}, 400

        result = EmailEventService.record(
            self.parse(payload),
            provider=self.key,
            provider_event_id=envelope["MessageId"],
        )
        return result, 200

    def parse(self, payload):
        """Wrap one SES notification in the reader the event service consumes."""
        from core.services.email_event_service import SESEventPayload

        return SESEventPayload(payload)
