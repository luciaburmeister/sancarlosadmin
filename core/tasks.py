import logging
from celery import shared_task
logger = logging.getLogger(__name__)


@shared_task(name="core.tasks.send_invoice_email_task")
def send_invoice_email_task(invoice_id: int, to_email: str | None = None):
    """
    Celery task to send a SalesInvoice email with PDF attachment.
    Runs asynchronously so the admin UI doesn't block.
    """
    from invoices.models import SalesInvoice
    from invoices.services.invoice_email_service import InvoiceEmailService

    try:
        invoice = SalesInvoice.objects.select_related("client", "company").get(pk=invoice_id)
    except SalesInvoice.DoesNotExist:
        logger.error("send_invoice_email_task: SalesInvoice %s not found", invoice_id)
        return False

    return InvoiceEmailService.send_invoice_email(invoice, to_email=to_email)


@shared_task(name="core.tasks.send_quote_email_task")
def send_quote_email_task(quote_id: int, to_email: str | None = None):
    """
    Celery task to send a Quote email with PDF attachment.
    Runs asynchronously so the admin UI doesn't block.
    """
    from sales.models import Quote
    from sales.services.quote_email_service import QuoteEmailService

    try:
        quote = Quote.objects.select_related("client", "company").get(pk=quote_id)
    except Quote.DoesNotExist:
        logger.error("send_quote_email_task: Quote %s not found", quote_id)
        return False

    return QuoteEmailService.send_quote_email(quote, to_email=to_email)


@shared_task(name="core.tasks.send_client_terms_email_task")
def send_client_terms_email_task(client_id: int, to_email: str | None = None):
    """
    Celery task to email signed T&C acceptance to the client.
    """
    from sales.models import Client
    from sales.services.client_terms_email_service import ClientTermsEmailService

    try:
        client = Client.objects.select_related(
            "company", "accepted_terms_company"
        ).get(pk=client_id)
    except Client.DoesNotExist:
        logger.error(
            "send_client_terms_email_task: Client %s not found", client_id
        )
        return False

    return ClientTermsEmailService.send_terms_acceptance_email(
        client, to_email=to_email
    )
