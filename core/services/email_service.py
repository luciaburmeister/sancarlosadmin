import logging
import uuid
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django.contrib.contenttypes.models import ContentType
from email.mime.image import MIMEImage

from general.email import resolve_reply_to, tracking_policy
from general.email.providers import default_provider

logger = logging.getLogger(__name__)


class EnvelopeEmailMessage(EmailMessage):
    """Email whose visible recipients can differ from its SMTP envelope.

    Tracked documents are sent once per external recipient so an SES open
    event belongs to one person.  Keeping the original To/Cc headers on each
    copy preserves normal reply-all behaviour, while the envelope ensures each
    address receives only its intended copy.
    """

    def __init__(self, *args, envelope_recipients=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._envelope_recipients = (
            None if envelope_recipients is None else list(envelope_recipients)
        )

    def recipients(self):
        if self._envelope_recipients is None:
            return super().recipients()
        return list(self._envelope_recipients)


class EmailService:
    """
    Generic, reusable email service with support for HTML bodies and
    file attachments (e.g. PDF invoices).
    """

    @staticmethod
    def send_email(
        to: list[str],
        subject: str,
        body_html: str,
        attachments: list[tuple] | None = None,
        inline_images: list[tuple] | None = None,
        from_email: str | None = None,
        cc: list[str] | None = None,
        related_object=None,
        company=None,
        email_document: str | None = None,
        reply_to: list[str] | None = None,
    ) -> bool:
        """
        Sends an HTML email with optional attachments and inline (CID) images.

        Args:
            to: List of recipient email addresses
            subject: Email subject line
            body_html: Rendered HTML body
            attachments: List of (filename, content_bytes, mimetype) tuples
            inline_images: List of (content_id, content_bytes, mimetype) tuples
                referenced from body_html via cid: (e.g. <img src="cid:logo">).
                content_id should NOT include angle brackets.
            from_email: Sender address (defaults to DEFAULT_FROM_EMAIL)
            cc: Optional list of CC email addresses
            related_object: Optional model instance for EmailLog (GFK)
            company: Optional company for EmailLog
            email_document: Registered document-type key (see general.email)
                used to look up the company's Reply-To for this kind of email
            reply_to: Explicit Reply-To addresses, winning over the company
                configuration — this is how a send triggered by a specific user
                routes replies back to that user

        Returns:
            True if sent successfully, False otherwise
        """
        message = {
            "subject": subject,
            "body_html": body_html,
            "sender": from_email or settings.DEFAULT_FROM_EMAIL,
            "reply_to": reply_to or resolve_reply_to(
                company=company, email_document=email_document
            ),
            "attachments": attachments,
            "inline_images": inline_images,
        }
        recipients = EmailService._unique_addresses(to)
        cc_recipients = EmailService._unique_addresses(cc or [])
        recipient_keys = {address.casefold() for address in recipients}
        cc_recipients = [
            address
            for address in cc_recipients
            if address.casefold() not in recipient_keys
        ]

        # Tracking needs a provider that is configured for this kind of email,
        # and something to hang the EmailLog's generic foreign key on.
        provider = default_provider()
        policy = tracking_policy(email_document)
        if provider is not None and provider.tracking_enabled(policy) and related_object is not None:
            return EmailService._send_tracked(
                message,
                recipients=recipients,
                cc_recipients=cc_recipients,
                provider=provider,
                policy=policy,
                related_object=related_object,
                company=company,
                email_document=email_document,
            )

        return EmailService._send_single(
            message,
            recipients=recipients,
            cc_recipients=cc_recipients,
            related_object=related_object,
            company=company,
            email_document=email_document,
        )

    @staticmethod
    def _send_tracked(
        message, *, recipients, cc_recipients, provider, policy, related_object,
        company, email_document,
    ):
        """
        One delivery per recipient, each individually attributable.

        Open events belong to a message, not reliably to one address when a
        message has several To/Cc recipients.  Tracked sends are therefore
        deliberately fanned out one recipient at a time.  Every copy preserves
        the original visible To/Cc headers, but its SMTP envelope contains only
        the intended recipient.  The employee's copy is untracked, so opening
        it can never be reported as the client opening the original.
        """
        batch_id = uuid.uuid4()
        results = []
        for recipient in recipients:
            log = EmailService._create_tracked_log(
                recipient=recipient,
                subject=message["subject"],
                related_object=related_object,
                company=company,
                email_document=email_document,
                batch_id=batch_id,
                provider=provider.key,
            )
            extra_headers = {}
            if log is not None:
                extra_headers = provider.outgoing_headers(
                    log=log, batch_id=batch_id, policy=policy
                )

            msg = EmailService._build_message(
                **message,
                to=recipients,
                cc=cc_recipients,
                envelope_recipients=[recipient],
                extra_headers=extra_headers,
            )
            results.append(
                EmailService._send_tracked_message(msg, log, recipient, message["subject"])
            )

        if cc_recipients and any(results):
            EmailService._send_audit_copy(message, recipients, cc_recipients)

        return bool(results) and all(results)

    @staticmethod
    def _send_audit_copy(message, recipients, cc_recipients):
        """The internal copy: one message, no tracking headers, no EmailLog."""
        audit_copy = EmailService._build_message(
            **message,
            to=recipients,
            cc=cc_recipients,
            envelope_recipients=cc_recipients,
        )
        try:
            audit_copy.send(fail_silently=False)
        except Exception:
            # The client delivery already succeeded.  A failed internal audit
            # copy must be visible in logs but must not turn the customer send
            # into a false failure.
            logger.exception(
                "Client email sent, but audit copy failed for %s: %s",
                cc_recipients,
                message["subject"],
            )

    @staticmethod
    def _send_single(
        message, *, recipients, cc_recipients, related_object, company, email_document,
    ):
        """
        One message to everyone, the way this service has always sent.

        The path for every email whose document type does not opt into
        tracking, and the fallback while no provider is configured.
        """
        msg = EmailService._build_message(
            **message, to=recipients, cc=cc_recipients,
        )

        error_message = ""
        status = "success"
        try:
            msg.send(fail_silently=False)
            logger.info(
                "Email sent to %s (cc: %s): %s",
                recipients, cc_recipients, message["subject"],
            )
        except Exception as exc:
            error_message = str(exc)
            status = "failed"
            logger.exception(
                "Failed to send email to %s: %s", recipients, message["subject"]
            )

        EmailService._create_send_log(
            recipients=recipients,
            cc=cc_recipients,
            subject=message["subject"],
            status=status,
            error_message=error_message,
            related_object=related_object,
            company=company,
            email_document=email_document,
        )
        return status == "success"

    @staticmethod
    def _unique_addresses(addresses):
        unique = []
        seen = set()
        for address in addresses or []:
            value = (address or "").strip()
            normalized = value.lower()
            if value and normalized not in seen:
                seen.add(normalized)
                unique.append(value)
        return unique

    @staticmethod
    def _build_message(
        *, subject, body_html, sender, to, cc, reply_to, attachments,
        inline_images, extra_headers=None, envelope_recipients=None,
    ):
        msg = EnvelopeEmailMessage(
            subject=subject,
            body=strip_tags(body_html),
            from_email=sender,
            to=to,
            cc=cc,
            reply_to=reply_to,
            headers=extra_headers or None,
            envelope_recipients=envelope_recipients,
        )
        msg.content_subtype = "html"
        msg.body = body_html

        if inline_images:
            msg.mixed_subtype = "related"
            for content_id, content, mimetype in inline_images:
                subtype = mimetype.split("/")[-1] if "/" in mimetype else "png"
                image = MIMEImage(content, _subtype=subtype)
                image.add_header("Content-ID", f"<{content_id}>")
                image.add_header(
                    "Content-Disposition", "inline", filename=f"{content_id}.{subtype}"
                )
                msg.attach(image)

        if attachments:
            for filename, content, mimetype in attachments:
                msg.attach(filename, content, mimetype)
        return msg

    @staticmethod
    def _create_tracked_log(
        *, recipient, subject, related_object, company, email_document, batch_id,
        provider,
    ):
        try:
            from company.models import EmailLog

            with transaction.atomic():
                return EmailLog.objects.create(
                    company=company,
                    content_type=ContentType.objects.get_for_model(related_object),
                    object_id=related_object.pk,
                    batch_id=batch_id,
                    email_document=email_document or "",
                    to_email=recipient,
                    subject=subject,
                    status=EmailLog.EmailStatus.PENDING,
                    tracking_enabled=True,
                    provider=provider,
                )
        except Exception:
            logger.exception("Failed to create tracked EmailLog entry")
            return None

    @staticmethod
    def _send_tracked_message(msg, log, recipient, subject):
        try:
            msg.send(fail_silently=False)
        except Exception as exc:
            logger.exception("Failed to send tracked email to %s: %s", recipient, subject)
            if log is not None:
                try:
                    log.status = log.EmailStatus.FAILED
                    log.error_message = str(exc)
                    with transaction.atomic():
                        log.save(update_fields=["status", "error_message"])
                except Exception:
                    logger.exception("Failed to mark tracked EmailLog as failed")
            return False

        logger.info("Tracked email accepted for %s: %s", recipient, subject)
        if log is not None:
            try:
                now = timezone.now()
                log.status = log.EmailStatus.SUCCESS
                log.delivery_status = log.DeliveryStatus.ACCEPTED
                log.accepted_at = now
                with transaction.atomic():
                    log.save(update_fields=["status", "delivery_status", "accepted_at"])
            except Exception:
                # The provider already accepted the message, so callers must
                # not retry and accidentally send it twice just because the
                # local summary could not be updated.
                logger.exception("Tracked email sent but EmailLog could not be updated")
        return True

    @staticmethod
    def _create_send_log(
        *, recipients, cc, subject, status, error_message, related_object,
        company, email_document,
    ):
        if related_object is None:
            return
        try:
            from company.models import EmailLog

            log_to = ", ".join(recipients)
            if cc:
                log_to += " | CC: " + ", ".join(cc)
            with transaction.atomic():
                EmailLog.objects.create(
                    company=company,
                    content_type=ContentType.objects.get_for_model(related_object),
                    object_id=related_object.pk,
                    email_document=email_document or "",
                    to_email=log_to,
                    subject=subject,
                    status=status,
                    error_message=error_message,
                )
        except Exception:
            logger.exception("Failed to create EmailLog entry")

    @staticmethod
    def render_document_email(
        document_type: str,
        document_number: str,
        company_name: str,
        client_name: str,
        total: str,
        extra_context: dict | None = None,
    ) -> str:
        """
        Renders the standard document email body.

        Args:
            document_type: e.g. "Factura", "Presupuesto"
            document_number: e.g. "INV-2026-00001", "Presupuesto #42"
            company_name: Sending company's name
            client_name: Recipient client's name
            total: Formatted total amount string
            extra_context: Additional template context

        Returns:
            Rendered HTML string
        """
        context = {
            "document_type": document_type,
            "document_number": document_number,
            "company_name": company_name,
            "client_name": client_name,
            "total": total,
        }
        if extra_context:
            context.update(extra_context)

        return render_to_string("email/document_sent.html", context)
