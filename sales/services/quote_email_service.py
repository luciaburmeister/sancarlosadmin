import logging
from django.utils.formats import number_format

from core.services.pdf_service import PDFService
from core.services.email_service import EmailService
from configurator.models import ExportTemplateKind

logger = logging.getLogger(__name__)


class QuoteEmailService:
    """
    Service for emailing quotes (proposals) as PDF attachments.
    Reuses the existing export template system for PDF generation.
    """

    @staticmethod
    def send_quote_email(
        quote,
        to_emails: list[str] | None = None,
        cc_emails: list[str] | None = None,
    ) -> bool:
        """
        Generates a PDF from the quote's consolidated quotation export template
        and emails it to the client.

        Args:
            quote: Quote instance
            to_emails: List of recipient addresses (defaults to [quote.client.email])
            cc_emails: List of CC addresses (e.g. the sender's own email)

        Returns:
            True if sent successfully, False otherwise
        """
        recipients = to_emails or (
            [quote.client.email] if quote.client and quote.client.email else []
        )
        if not recipients:
            logger.error(
                "Cannot send quote email: no email for quote %s (client: %s)",
                quote.pk, quote.client,
            )
            return False

        # Generate PDF from existing export template
        pdf_bytes = PDFService.render_object_pdf(
            quote,
            ExportTemplateKind.CONSOLIDATED_QUOTATION,
            company=quote.company,
        )

        if not pdf_bytes:
            logger.error(
                "Cannot send quote email: no PDF template found for quote %s",
                quote.pk,
            )
            return False

        # Build email body
        doc_number = f"Presupuesto #{quote.id}"
        total_str = f"{number_format(quote.total_price.amount, 2)} €"

        body_html = EmailService.render_document_email(
            document_type="Presupuesto",
            document_number=doc_number,
            company_name=quote.company.name if quote.company else "—",
            client_name=quote.client.name if quote.client else "—",
            total=total_str,
            extra_context={
                "expires_at": quote.expires_at,
            },
        )

        filename = f"Presupuesto_{quote.id}.pdf"

        return EmailService.send_email(
            to=recipients,
            subject=f"{quote.company.name} — {doc_number}",
            body_html=body_html,
            attachments=[(filename, pdf_bytes, "application/pdf")],
            cc=cc_emails or [],
            related_object=quote,
            company=quote.company,
        )
