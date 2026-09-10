import logging
from django.utils.formats import number_format

from core.services.pdf_service import PDFService
from core.services.email_service import EmailService
from configurator.models import ExportTemplateKind

logger = logging.getLogger(__name__)


class InvoiceEmailService:
    """
    Service for emailing sales invoices as PDF attachments.
    Reuses the existing export template system for PDF generation.
    """

    @staticmethod
    def send_invoice_email(
        invoice,
        to_email: str | None = None,
        reply_to: list[str] | None = None,
    ) -> bool:
        """
        Generates a PDF from the invoice's export template and emails it
        to the client.

        Args:
            invoice: SalesInvoice instance
            to_email: Override recipient (defaults to invoice.client.email)
            reply_to: Sending user's address, if a person triggered this send
                (otherwise the company's configured Reply-To applies)

        Returns:
            True if sent successfully, False otherwise
        """
        recipient = to_email or (invoice.client.email if invoice.client else None)
        if not recipient:
            logger.error(
                "Cannot send invoice email: no email for invoice %s (client: %s)",
                invoice.pk, invoice.client,
            )
            return False

        # Generate PDF from existing export template
        pdf_bytes = PDFService.render_object_pdf(
            invoice,
            ExportTemplateKind.SALES_INVOICE,
            company=invoice.company,
        )

        if not pdf_bytes:
            logger.error(
                "Cannot send invoice email: no PDF template found for invoice %s",
                invoice.pk,
            )
            return False

        # Build email body
        doc_number = invoice.invoice_number or f"Pro Forma #{invoice.id}"
        total_str = f"{number_format(invoice.total_amount.amount, 2)} €"

        body_html = EmailService.render_document_email(
            document_type="Factura",
            document_number=doc_number,
            company_name=invoice.company.name if invoice.company else "—",
            client_name=invoice.client.name if invoice.client else "—",
            total=total_str,
            extra_context={
                "date_issued": invoice.date_issued,
                "due_date": invoice.due_date,
            },
        )

        filename = f"{doc_number.replace(' ', '_')}.pdf"

        return EmailService.send_email(
            to=[recipient],
            subject=f"{invoice.company.name} — {doc_number}",
            body_html=body_html,
            attachments=[(filename, pdf_bytes, "application/pdf")],
            related_object=invoice,
            company=invoice.company,
            email_document="sales_invoice",
            reply_to=reply_to,
        )
