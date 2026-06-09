"""
Celery tasks for asynchronous e-invoicing operations.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def submit_invoice_to_aeat(self, sales_invoice_id: int):
    """Submit a finalized SalesInvoice to the AEAT via the configured provider."""
    from invoices.models import SalesInvoice, SalesInvoiceStatus
    from apps.einvoicing.services import EInvoicingService

    try:
        invoice = SalesInvoice.objects.select_related("company", "client").get(
            pk=sales_invoice_id
        )
    except SalesInvoice.DoesNotExist:
        
        return ("SalesInvoice %s does not exist — aborting.", sales_invoice_id)

    if invoice.status == SalesInvoiceStatus.CANCELLED:
        logger.warning(
            "SalesInvoice %s is cancelled — cannot resubmit.",
            sales_invoice_id,
        )
        return

    if invoice.status != SalesInvoiceStatus.FINAL:
        logger.warning(
            "SalesInvoice %s is not finalized (status=%s) — skipping.",
            sales_invoice_id,
            invoice.status,
        )
        return

    submission = EInvoicingService.submit_invoice(invoice)

    if submission is None:
        logger.info("No e-invoicing account configured — nothing to do.")
        return

    if not submission.status == "accepted":
        logger.warning(
            "Submission for invoice %s got status '%s': %s. Will retry.",
            sales_invoice_id,
            submission.status,
            submission.error_message,
        )
        if submission.status == "error":
            raise self.retry(exc=Exception(submission.error_message))


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def cancel_invoice_at_aeat(self, sales_invoice_id: int):
    """Cancel a SalesInvoice at the AEAT via the configured provider."""
    from invoices.models import SalesInvoice, SalesInvoiceStatus
    from apps.einvoicing.services import EInvoicingService

    try:
        invoice = SalesInvoice.objects.select_related("company", "client").get(
            pk=sales_invoice_id
        )
    except SalesInvoice.DoesNotExist:
        logger.error("SalesInvoice %s does not exist — aborting.", sales_invoice_id)
        return

    if invoice.status == SalesInvoiceStatus.CANCELLED:
        logger.warning(
            "SalesInvoice %s is already cancelled — skipping.",
            sales_invoice_id,
        )
        return

    submission = EInvoicingService.cancel_invoice(invoice)

    if submission is None:
        logger.info("No e-invoicing account configured — nothing to do.")
        return

    if submission.status == "error":
        raise self.retry(exc=Exception(submission.error_message))
