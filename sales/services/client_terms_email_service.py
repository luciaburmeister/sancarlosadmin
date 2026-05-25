import logging

from django.template.loader import render_to_string
from django.utils import formats, timezone

from core.services.email_service import EmailService

logger = logging.getLogger(__name__)


class ClientTermsEmailService:
    """Email signed T&C acceptance to the client."""

    @staticmethod
    def send_terms_acceptance_email(client, to_email: str | None = None) -> bool:
        """
        Sends accepted terms text and signature image to the client.

        Args:
            client: Client with terms_accepted_at and accepted_terms_text set
            to_email: Override recipient (defaults to client.email)

        Returns:
            True if sent successfully, False otherwise
        """
        recipient = (to_email or (client.email or "")).strip()
        if not recipient:
            logger.info(
                "Skipping terms email for client %s: no email address",
                client.pk,
            )
            return False

        terms_text = (client.accepted_terms_text or "").strip()
        if not terms_text or not client.terms_accepted_at:
            logger.error(
                "Cannot send terms email: client %s has no accepted terms snapshot",
                client.pk,
            )
            return False

        company = client.accepted_terms_company or client.company
        company_name = company.name if company else "—"
        accepted_local = timezone.localtime(client.terms_accepted_at)
        accepted_at = formats.date_format(accepted_local, "SHORT_DATETIME_FORMAT")

        body_html = render_to_string(
            "email/client_terms_accepted.html",
            {
                "client_name": client.name,
                "company_name": company_name,
                "accepted_at": accepted_at,
                "terms_version": client.terms_version,
                "terms_html": terms_text,
            },
        )

        attachments = []
        if client.terms_signature:
            try:
                with client.terms_signature.open("rb") as sig_file:
                    attachments.append(
                        (
                            f"firma_terminos_{client.pk}.png",
                            sig_file.read(),
                            "image/png",
                        )
                    )
            except Exception:
                logger.exception(
                    "Failed to read terms signature for client %s", client.pk
                )

        subject = f"{company_name} — Términos y condiciones aceptados"

        return EmailService.send_email(
            to=[recipient],
            subject=subject,
            body_html=body_html,
            attachments=attachments or None,
            related_object=client,
            company=client.company,
        )

    @staticmethod
    def queue_terms_acceptance_email(client) -> bool:
        """Enqueue terms acceptance email via Celery when the client has an email."""
        email = (client.email or "").strip()
        if not email:
            return False
        if not client.terms_accepted_at or not (client.accepted_terms_text or "").strip():
            return False

        from core.tasks import send_client_terms_email_task

        try:
            send_client_terms_email_task.delay(client.pk)
            return True
        except Exception:
            logger.warning(
                "Celery unavailable for client %s terms email; sending synchronously",
                client.pk,
            )
            return ClientTermsEmailService.send_terms_acceptance_email(client)
