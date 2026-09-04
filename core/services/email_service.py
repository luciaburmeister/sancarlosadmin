import logging
from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.contrib.contenttypes.models import ContentType
from email.mime.image import MIMEImage

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
        inline_images: list[tuple] | None = None,
        from_email: str | None = None,
        cc: list[str] | None = None,
        related_object=None,
        company=None,
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
            cc=cc or [],
        )
        msg.content_subtype = "html"
        msg.body = body_html

        if inline_images:
            msg.mixed_subtype = "related"
            for content_id, content, mimetype in inline_images:
                subtype = mimetype.split("/")[-1] if "/" in mimetype else "png"
                image = MIMEImage(content, _subtype=subtype)
                image.add_header("Content-ID", f"<{content_id}>")
                image.add_header("Content-Disposition", "inline", filename=f"{content_id}.{subtype}")
                msg.attach(image)

        if attachments:
            for filename, content, mimetype in attachments:
                msg.attach(filename, content, mimetype)

        error_message = ""
        status = "success"
        try:
            msg.send(fail_silently=False)
            logger.info("Email sent to %s (cc: %s): %s", to, cc or [], subject)
        except Exception as e:
            error_message = str(e)
            status = "failed"
            logger.exception("Failed to send email to %s: %s", to, subject)

        # Log the email send attempt
        if related_object is not None:
            try:
                from company.models import EmailLog

                log_to = ", ".join(to)
                if cc:
                    log_to += " | CC: " + ", ".join(cc)

                ct = ContentType.objects.get_for_model(related_object)
                EmailLog.objects.create(
                    company=company,
                    content_type=ct,
                    object_id=related_object.pk,
                    to_email=log_to,
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