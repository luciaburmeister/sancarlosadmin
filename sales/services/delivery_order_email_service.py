import logging
from django.utils.formats import number_format

from core.services.pdf_service import PDFService
from core.services.email_service import EmailService
from configurator.models import ExportTemplateKind

logger = logging.getLogger(__name__)


class DeliveryOrderEmailService:
    """
    Service for emailing delivery orders (albaranes) as PDF attachments.
    Reuses the existing export template system for PDF generation.
    """

    @staticmethod
    def send_delivery_order_email(
        delivery_order,
        to_emails: list[str] | None = None,
        cc_emails: list[str] | None = None,
        reply_to: list[str] | None = None,
    ) -> bool:
        """
        Generates a PDF from the delivery order export template and emails
        it to the client. Marks the delivery order as sent on success.

        Args:
            delivery_order: DeliveryOrder instance
            to_emails: List of recipient addresses (defaults to [client.email])
            cc_emails: List of CC addresses (e.g. the sender's own email)
            reply_to: Sending user's address, if a person triggered this send
                (otherwise the company's configured Reply-To applies)

        Returns:
            True if sent successfully, False otherwise
        """
        recipients = to_emails or (
            [delivery_order.client.email]
            if delivery_order.client and delivery_order.client.email
            else []
        )
        if not recipients:
            logger.error(
                "Cannot send delivery order email: no email for delivery order %s (client: %s)",
                delivery_order.pk, delivery_order.client,
            )
            return False

        # Generate PDF from existing export template
        pdf_bytes = PDFService.render_object_pdf(
            delivery_order,
            ExportTemplateKind.DELIVERY_ORDER,
            company=delivery_order.company,
        )

        if not pdf_bytes:
            logger.error(
                "Cannot send delivery order email: no PDF template found for delivery order %s",
                delivery_order.pk,
            )
            return False

        # Build email body
        doc_number = (
            f"Albarán {delivery_order.number}"
            if delivery_order.number
            else f"Albarán #{delivery_order.pk}"
        )
        # Legacy delivery orders predate the total_amount field — fall back
        # to the subtotal there.
        total_money = (
            delivery_order.total_amount
            if delivery_order.total_amount and delivery_order.total_amount.amount > 0
            else delivery_order.subtotal_amount
        )
        total_str = f"{number_format(total_money.amount, 2)} €"

        body_html = EmailService.render_document_email(
            document_type="Albarán",
            document_number=doc_number,
            company_name=delivery_order.company.name if delivery_order.company else "—",
            client_name=delivery_order.client.name if delivery_order.client else "—",
            total=total_str,
            extra_context={
                "package_count": delivery_order.package_count,
                "shipping_agency": delivery_order.shipping_agency,
                "shipment_number": delivery_order.shipment_number,
                "tracking_link": delivery_order.tracking_link,
            },
        )

        filename = f"Albaran_{delivery_order.number or delivery_order.pk}.pdf"

        ok = EmailService.send_email(
            to=recipients,
            subject=f"{delivery_order.company.name} — {doc_number}",
            body_html=body_html,
            attachments=[(filename, pdf_bytes, "application/pdf")],
            cc=cc_emails or [],
            related_object=delivery_order,
            company=delivery_order.company,
            email_document="delivery_order",
            reply_to=reply_to,
        )

        # Emailing only notifies the client: the order is marked as shipped
        # by dispatching it, which is also what takes its stock out.
        return ok
