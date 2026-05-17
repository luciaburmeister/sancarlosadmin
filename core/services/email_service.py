import logging
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.contrib.contenttypes.models import ContentType

logger = logging.getLogger(__name__)


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
        from_email: str | None = None,
        related_object=None,
        company=None,
    ) -> bool:
        """
        Sends an HTML email with optional attachments.

        Args:
            to: List of recipient email addresses
            subject: Email subject line
            body_html: Rendered HTML body
            attachments: List of (filename, content_bytes, mimetype) tuples
            from_email: Sender address (defaults to DEFAULT_FROM_EMAIL)
            related_object: Optional model instance for EmailLog (GFK)
            company: Optional company for EmailLog

        Returns:
            True if sent successfully, False otherwise
        """
        sender = from_email or settings.DEFAULT_FROM_EMAIL
        plain_body = strip_tags(body_html)

        msg = EmailMessage(
            subject=subject,
            body=plain_body,
            from_email=sender,
            to=to,
        )
        msg.content_subtype = "html"
        msg.body = body_html

        if attachments:
            for filename, content, mimetype in attachments:
                msg.attach(filename, content, mimetype)

        error_message = ""
        status = "success"
        try:
            msg.send(fail_silently=False)
            logger.info("Email sent to %s: %s", to, subject)
        except Exception as e:
            error_message = str(e)
            status = "failed"
            logger.exception("Failed to send email to %s: %s", to, subject)

        # Log the email send attempt
        if related_object is not None:
            try:
                from company.models import EmailLog

                ct = ContentType.objects.get_for_model(related_object)
                EmailLog.objects.create(
                    company=company,
                    content_type=ct,
                    object_id=related_object.pk,
                    to_email=", ".join(to),
                    subject=subject,
                    status=status,
                    error_message=error_message,
                )
            except Exception:
                logger.exception("Failed to create EmailLog entry")

        return status == "success"

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
