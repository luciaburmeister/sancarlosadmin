import logging
from django.utils.translation import gettext_lazy as _

from core.services.pdf_service import PDFService
from core.services.email_service import EmailService
from configurator.models import ExportTemplateKind

logger = logging.getLogger(__name__)


class AssetTransactionEmailService:
    """
    Service for emailing asset transaction documents as PDF attachments.
    Resolves the recipient from the Expediente's client (with fallback to
    the asset's client) and reuses the existing export template system.
    """

    @staticmethod
    def send_transaction_email(transaction, to_email: str | None = None) -> bool:
        """
        Generates a PDF from the transaction's export template and emails it.

        Args:
            transaction: AssetTransaction instance (with relations prefetched)
            to_email: Override recipient (defaults to project_file.client.email
                      or asset.client.email)

        Returns:
            True if sent successfully, False otherwise
        """
        # Resolve recipient
        recipient = to_email
        if not recipient:
            if transaction.project_file:
                recipient = (
                    transaction.project_file.notification_email
                    or (transaction.project_file.client and transaction.project_file.client.email)
                )
            if not recipient and transaction.asset and transaction.asset.client:
                recipient = transaction.asset.client.email

        if not recipient:
            logger.error(
                "Cannot send transaction email: no email for transaction %s",
                transaction.pk,
            )
            return False

        # Generate PDF from existing export template
        pdf_bytes = PDFService.render_object_pdf(
            transaction,
            ExportTemplateKind.ASSET_TRANSACTION,
            company=transaction.company,
        )

        if not pdf_bytes:
            logger.error(
                "Cannot send transaction email: no PDF template found for transaction %s",
                transaction.pk,
            )
            return False

        # Resolve client name for the email body
        client_name = "—"
        if transaction.project_file and transaction.project_file.client:
            client_name = transaction.project_file.client.name
        elif transaction.asset and transaction.asset.client:
            client_name = transaction.asset.client.name

        # Build email body
        asset_label = str(transaction.asset) if transaction.asset else f"#{transaction.pk}"
        tx_type = transaction.get_transaction_type_display()
        doc_label = f"{tx_type} — {asset_label}"

        body_html = EmailService.render_document_email(
            document_type=_("Asset Transaction"),
            document_number=doc_label,
            company_name=transaction.company.name if transaction.company else "—",
            client_name=client_name,
            total="",
            extra_context={
                "date": transaction.date,
                "from_zone": str(transaction.from_zone) if transaction.from_zone else None,
                "to_zone": str(transaction.to_zone) if transaction.to_zone else None,
            },
        )

        filename = f"transaction_{transaction.pk}.pdf"

        return EmailService.send_email(
            to=[recipient],
            subject=f"{transaction.company.name} — {doc_label}",
            body_html=body_html,
            attachments=[(filename, pdf_bytes, "application/pdf")],
            related_object=transaction,
            company=transaction.company,
        )
