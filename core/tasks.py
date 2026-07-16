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
        return {"task": "send_invoice_email", "invoice_id": invoice_id, "status": "error", "detail": "not found"}

    ok = InvoiceEmailService.send_invoice_email(invoice, to_email=to_email)
    return {
        "task": "send_invoice_email",
        "invoice_id": invoice_id,
        "to": to_email or str(invoice.client),
        "status": "ok" if ok else "error",
    }


@shared_task(name="core.tasks.send_quote_email_task")
def send_quote_email_task(
    quote_id: int,
    to_emails: list[str] | None = None,
    cc_emails: list[str] | None = None,
):
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
        return {"task": "send_quote_email", "quote_id": quote_id, "status": "error", "detail": "not found"}

    ok = QuoteEmailService.send_quote_email(
        quote, to_emails=to_emails, cc_emails=cc_emails,
    )
    return {
        "task": "send_quote_email",
        "quote_id": quote_id,
        "to": to_emails or str(quote.client),
        "cc": cc_emails or [],
        "status": "ok" if ok else "error",
    }


@shared_task(name="core.tasks.send_delivery_order_email_task")
def send_delivery_order_email_task(
    delivery_order_id: int,
    to_emails: list[str] | None = None,
    cc_emails: list[str] | None = None,
):
    """
    Celery task to send a DeliveryOrder email with PDF attachment.
    Runs asynchronously so the admin UI doesn't block.
    """
    from sales.models import DeliveryOrder
    from sales.services.delivery_order_email_service import DeliveryOrderEmailService

    try:
        delivery_order = DeliveryOrder.objects.select_related("client", "company").get(pk=delivery_order_id)
    except DeliveryOrder.DoesNotExist:
        logger.error("send_delivery_order_email_task: DeliveryOrder %s not found", delivery_order_id)
        return {"task": "send_delivery_order_email", "delivery_order_id": delivery_order_id, "status": "error", "detail": "not found"}

    ok = DeliveryOrderEmailService.send_delivery_order_email(
        delivery_order, to_emails=to_emails, cc_emails=cc_emails,
    )
    return {
        "task": "send_delivery_order_email",
        "delivery_order_id": delivery_order_id,
        "to": to_emails or str(delivery_order.client),
        "cc": cc_emails or [],
        "status": "ok" if ok else "error",
    }


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
        return {"task": "send_client_terms_email", "client_id": client_id, "status": "error", "detail": "not found"}

    ok = ClientTermsEmailService.send_terms_acceptance_email(
        client, to_email=to_email
    )
    return {
        "task": "send_client_terms_email",
        "client_id": client_id,
        "to": to_email or str(client),
        "status": "ok" if ok else "error",
    }


@shared_task(name="core.tasks.send_asset_transaction_email_task")
def send_asset_transaction_email_task(transaction_id: int, to_email: str | None = None):
    """
    Celery task to send an AssetTransaction email with PDF attachment.
    Runs asynchronously so the admin UI doesn't block.
    """
    from assets.models import AssetTransaction
    from assets.services.asset_transaction_email_service import AssetTransactionEmailService

    try:
        transaction = AssetTransaction.objects.select_related(
            "asset", "asset__client", "asset__asset_type",
            "company", "from_zone", "to_zone",
            "project_file", "project_file__client",
            "performed_by",
        ).get(pk=transaction_id)
    except AssetTransaction.DoesNotExist:
        logger.error("send_asset_transaction_email_task: AssetTransaction %s not found", transaction_id)
        return {"task": "send_asset_transaction_email", "transaction_id": transaction_id, "status": "error", "detail": "not found"}

    ok = AssetTransactionEmailService.send_transaction_email(transaction, to_email=to_email)
    return {
        "task": "send_asset_transaction_email",
        "transaction_id": transaction_id,
        "to": to_email or str(transaction.asset),
        "status": "ok" if ok else "error",
    }


@shared_task(name="core.tasks.generate_work_entries_task")
def generate_work_entries_task(transaction_ids: list[int]):
    """
    Celery task to generate all missing work entries for a list of
    AssetTransaction IDs. Runs asynchronously so bulk actions don't block.
    """
    from assets.models import AssetTransaction, BillingStatus
    from management.services.work_entry import WorkEntryService

    total_created = 0
    total_skipped = 0

    transactions = AssetTransaction.objects.filter(
        pk__in=transaction_ids,
    ).select_related("project_file", "company")

    for transaction in transactions:
        if not transaction.project_file:
            total_skipped += 1
            continue

        created = 0
        errors = 0
        try:
            status = WorkEntryService.get_billing_status(transaction)
            for wtype in status["missing"]:
                try:
                    WorkEntryService.create_work_entry_from_source(
                        entry_type=wtype,
                        source_object=transaction,
                        project_file=transaction.project_file,
                    )
                    created += 1
                    total_created += 1
                except Exception as e:
                    errors += 1
                    logger.exception(
                        "generate_work_entries_task: failed '%s' for tx %s: %s",
                        wtype.name, transaction.pk, e,
                    )
        except Exception as e:
            errors += 1
            logger.exception(
                "generate_work_entries_task: error processing transaction %s: %s",
                transaction.pk, e,
            )

        # Update billing status
        if errors and not created:
            transaction.billing_status = BillingStatus.FAILED
        elif errors:
            transaction.billing_status = BillingStatus.PENDING
        elif not status.get("missing"):
            # Nothing was missing — already fully processed
            transaction.billing_status = BillingStatus.PROCESSED
        elif created:
            remaining = len(status["missing"]) - created
            transaction.billing_status = BillingStatus.PROCESSED if remaining == 0 else BillingStatus.PENDING
        transaction.save(update_fields=["billing_status"])

    logger.info(
        "generate_work_entries_task: created %d entries, skipped %d transactions",
        total_created, total_skipped,
    )
    return {
        "task": "generate_work_entries",
        "created": total_created,
        "skipped": total_skipped,
    }
