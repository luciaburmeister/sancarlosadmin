"""Apply Amazon SES event-publishing notifications to outgoing email logs."""

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from company.models import EmailEvent, EmailLog


class SESEventPayload:
    """
    Read-only view over one SES notification.

    Knows the shape of Amazon's JSON and nothing about our models, so it can be
    exercised with a plain dict and no database.
    """

    _EVENT_TYPES = {
        "send": EmailEvent.EventType.SEND,
        "delivery": EmailEvent.EventType.DELIVERY,
        "deliverydelay": EmailEvent.EventType.DELIVERY_DELAY,
        "bounce": EmailEvent.EventType.BOUNCE,
        "complaint": EmailEvent.EventType.COMPLAINT,
        "reject": EmailEvent.EventType.REJECT,
        "renderingfailure": EmailEvent.EventType.RENDERING_FAILURE,
        "open": EmailEvent.EventType.OPEN,
        "click": EmailEvent.EventType.CLICK,
        "subscription": EmailEvent.EventType.SUBSCRIPTION,
    }
    # Sections whose name in the payload differs from the event key.
    _TIMESTAMP_SECTIONS = {
        "deliverydelay": "deliveryDelay",
        "renderingfailure": "failure",
    }
    _RECIPIENT_SECTIONS = {
        "delivery": ("delivery", "recipients"),
        "bounce": ("bounce", "bouncedRecipients"),
        "complaint": ("complaint", "complainedRecipients"),
        "deliverydelay": ("deliveryDelay", "delayedRecipients"),
    }

    def __init__(self, payload):
        self._payload = payload or {}
        self._key = self._normalize(
            self._payload.get("eventType") or self._payload.get("notificationType")
        )

    @staticmethod
    def _normalize(value):
        return "".join(character for character in (value or "").lower() if character.isalnum())

    def _section(self, name):
        section = self._payload.get(name, {})
        return section if isinstance(section, dict) else {}

    @property
    def _mail(self):
        return self._section("mail")

    @property
    def event_type(self):
        """The matching ``EmailEvent.EventType``, or ``None`` when unsupported."""
        return self._EVENT_TYPES.get(self._key)

    @property
    def message_id(self):
        return self._mail.get("messageId", "")

    @property
    def occurred_at(self):
        section = self._section(self._TIMESTAMP_SECTIONS.get(self._key, self._key))
        value = section.get("timestamp") or self._mail.get("timestamp")
        parsed = parse_datetime(value) if value else None
        return parsed or timezone.now()

    @property
    def recipient(self):
        section_name, list_name = self._RECIPIENT_SECTIONS.get(self._key, (None, None))
        rows = self._section(section_name).get(list_name, []) if section_name else []
        if rows:
            first = rows[0]
            return first if isinstance(first, str) else first.get("emailAddress", "")
        destinations = self._mail.get("destination", [])
        return destinations[0] if len(destinations) == 1 else ""

    @property
    def details(self):
        """Human-readable reason for the failure events, empty for the rest."""
        if self._key == "bounce":
            bounce = self._section("bounce")
            recipient = (bounce.get("bouncedRecipients") or [{}])[0]
            diagnostic = recipient.get("diagnosticCode", "") if isinstance(recipient, dict) else ""
            classification = "/".join(
                value for value in (bounce.get("bounceType"), bounce.get("bounceSubType")) if value
            )
            return " — ".join(value for value in (classification, diagnostic) if value)
        if self._key == "complaint":
            return self._section("complaint").get("complaintFeedbackType", "")
        if self._key == "reject":
            return self._section("reject").get("reason", "")
        if self._key == "renderingfailure":
            return self._section("failure").get("errorMessage", "")
        if self._key == "deliverydelay":
            return self._section("deliveryDelay").get("delayType", "")
        return ""

    @property
    def email_log_id(self):
        """The ``EmailLog`` primary key we tagged the message with, when present."""
        values = self._mail.get("tags", {}).get("erp_email_log_id", [])
        if isinstance(values, str):
            values = [values]
        if not values:
            return None
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return None


class EmailLogDeliveryState:
    """
    Applies one event to an ``EmailLog``'s summary fields.

    Owns the transitions: which event wins over which, and which timestamps are
    kept. Bounces, complaints and rejections are terminal, so a delivery
    notification arriving late never overwrites them.
    """

    TERMINAL = {
        EmailLog.DeliveryStatus.BOUNCED,
        EmailLog.DeliveryStatus.COMPLAINT,
        EmailLog.DeliveryStatus.REJECTED,
    }

    def __init__(self, log):
        self._log = log
        self._update_fields = []

    def _set(self, field, value):
        setattr(self._log, field, value)
        if field not in self._update_fields:
            self._update_fields.append(field)

    def _fail(self, delivery_status, details):
        self._set("delivery_status", delivery_status)
        self._set("delivery_error", details)

    def apply(self, event_type, occurred_at, details, message_id):
        """Record the event on the log and persist only the fields it touched."""
        if message_id and self._log.provider_message_id != message_id:
            self._set("provider_message_id", message_id)
        if self._log.last_event_at is None or occurred_at > self._log.last_event_at:
            self._set("last_event_at", occurred_at)

        handler = {
            EmailEvent.EventType.SEND: self._on_send,
            EmailEvent.EventType.DELIVERY_DELAY: self._on_delivery_delay,
            EmailEvent.EventType.DELIVERY: self._on_delivery,
            EmailEvent.EventType.BOUNCE: self._on_bounce,
            EmailEvent.EventType.COMPLAINT: self._on_complaint,
            EmailEvent.EventType.REJECT: self._on_rejection,
            EmailEvent.EventType.RENDERING_FAILURE: self._on_rejection,
            EmailEvent.EventType.OPEN: self._on_open,
        }.get(event_type)
        if handler is not None:
            handler(occurred_at, details)

        self._save()

    def _on_send(self, occurred_at, details):
        if self._log.delivery_status == EmailLog.DeliveryStatus.UNKNOWN:
            self._set("delivery_status", EmailLog.DeliveryStatus.ACCEPTED)
        if self._log.accepted_at is None:
            self._set("accepted_at", occurred_at)

    def _on_delivery_delay(self, occurred_at, details):
        if self._log.delivery_status not in self.TERMINAL | {EmailLog.DeliveryStatus.DELIVERED}:
            self._set("delivery_status", EmailLog.DeliveryStatus.DELAYED)

    def _on_delivery(self, occurred_at, details):
        if self._log.delivery_status not in self.TERMINAL:
            self._set("delivery_status", EmailLog.DeliveryStatus.DELIVERED)
        if self._log.delivered_at is None or occurred_at < self._log.delivered_at:
            self._set("delivered_at", occurred_at)

    def _on_bounce(self, occurred_at, details):
        self._fail(EmailLog.DeliveryStatus.BOUNCED, details)

    def _on_complaint(self, occurred_at, details):
        self._fail(EmailLog.DeliveryStatus.COMPLAINT, details)

    def _on_rejection(self, occurred_at, details):
        self._fail(EmailLog.DeliveryStatus.REJECTED, details)

    def _on_open(self, occurred_at, details):
        if self._log.first_opened_at is None or occurred_at < self._log.first_opened_at:
            self._set("first_opened_at", occurred_at)
        if self._log.last_opened_at is None or occurred_at > self._log.last_opened_at:
            self._set("last_opened_at", occurred_at)
        self._set("open_count", self._log.open_count + 1)

    def _save(self):
        if self._update_fields:
            self._log.save(update_fields=self._update_fields)


class EmailEventService:
    """
    Entry point for provider webhooks: locate the log, deduplicate, apply.

    Provider-neutral by design — it consumes whatever reader the provider hands
    it, so long as that reader exposes `event_type`, `occurred_at`, `recipient`,
    `details`, `message_id` and `email_log_id`.
    """

    @classmethod
    @transaction.atomic
    def record(cls, event, *, provider, provider_event_id):
        """Persist one provider event. Repeated deliveries are harmless."""
        if event.event_type is None:
            return {"status": "ignored", "reason": "unsupported event"}

        log = cls._find_log(event, provider)
        if log is None:
            return {"status": "ignored", "reason": "email log not found"}

        occurred_at = event.occurred_at
        details = event.details
        _, created = EmailEvent.objects.get_or_create(
            provider=provider,
            provider_event_id=provider_event_id,
            defaults={
                "email_log": log,
                "event_type": event.event_type,
                "occurred_at": occurred_at,
                "recipient": event.recipient,
                "details": details,
            },
        )
        if not created:
            return {"status": "duplicate"}

        EmailLogDeliveryState(log).apply(
            event.event_type,
            occurred_at,
            details,
            event.message_id,
        )
        return {"status": "processed", "event": event.event_type, "email_log_id": log.pk}

    @staticmethod
    def _find_log(event, provider):
        """Prefer the log id we tagged the message with, fall back to the provider's."""
        queryset = EmailLog.objects.select_for_update()
        if event.email_log_id is not None:
            return queryset.filter(pk=event.email_log_id, tracking_enabled=True).first()
        if event.message_id:
            return queryset.filter(
                provider=provider, provider_message_id=event.message_id
            ).first()
        return None
