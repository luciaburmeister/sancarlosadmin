from celery import shared_task
from django.conf import settings
from .models import IncomingInvoice, Invoice
from invoices.services.invoice.invoice_service import InvoiceService
from invoices.services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
import logging
import tempfile
import os
import shutil
from company.models import CompanyEmailConfig
from .services.email_processor import EmailInvoiceProcessor

logger = logging.getLogger(__name__)

@shared_task(name="invoices.tasks.fetch_email_invoices")
def fetch_email_invoices_task(config_id=None):
    """
    Background task to fetch invoices from configured email accounts.
    If config_id is provided, only that configuration is processed.
    """
    if config_id:
        configs = CompanyEmailConfig.objects.filter(id=config_id)
    else:
        configs = CompanyEmailConfig.objects.all()

    if not configs.exists():
        logger.info("No email configurations to process.")
        return {"task": "fetch_email_invoices", "status": "skipped", "detail": "no configurations"}

    total_count = 0
    for config in configs:
        logger.info(f"Fetching emails for {config.email_address} ({config.company})")
        processor = EmailInvoiceProcessor(config)
        try:
            processor.connect()
            count = processor.fetch_new_invoices()
            processor.disconnect()
            total_count += count
        except Exception as e:
            logger.error(f"Error fetching emails for {config.email_address}: {str(e)}")
            continue

    return {"task": "fetch_email_invoices", "status": "ok", "fetched": total_count}

#Email Only Pipeline
@shared_task(name="invoices.tasks.process_incoming_invoices")
def process_incoming_invoices(invoice_ids=None):
    """
    Background task to process IncomingInvoice records.
    If invoice_ids is provided, processes those specific records.
    Otherwise, processes all unprocessed records.
    """
    if invoice_ids:
        invoices = IncomingInvoice.objects.filter(id__in=invoice_ids, processed=False)
    else:
        invoices = IncomingInvoice.objects.filter(processed=False)
    
    if not invoices.exists():
        logger.info("No new invoices to process.")
        return {"task": "process_incoming_invoices", "status": "skipped", "detail": "no new invoices"}

    logger.info(f"Found {invoices.count()} invoices to process.")
    
    # Initialize services
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        return {"task": "process_incoming_invoices", "status": "error", "detail": "missing API key"}
        
    ocr_processor = GeminiInvoiceProcessor(api_key=api_key)
    service = InvoiceService(ocr_processor=ocr_processor)
    
    processed_count = 0
    errors_count = 0
    skipped_count = 0
    
    
    
    for incoming in invoices:
        temp_file_path = None
        try:
            logger.info(f"Processing invoice {incoming.id} (File: {incoming.file.name})")
            
            # Try to get local path, or download to temp
            try:
                file_path = incoming.file.path
            except NotImplementedError:
                suffix = os.path.splitext(incoming.file.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    with incoming.file.open('rb') as f:
                        shutil.copyfileobj(f, tmp)
                    temp_file_path = tmp.name
                    file_path = temp_file_path
            result = service.process_invoice(file_path, sender_email=incoming.sender_email)
            
            if result.success:
                incoming.processed = True
                incoming.is_duplicate = getattr(result, 'is_duplicate', False)
                incoming.save()
                processed_count += 1
                logger.info(f"Successfully processed invoice {incoming.id} (Duplicate: {incoming.is_duplicate})")
            elif result.invoice is None:
                # Document is not an invoice (quote, delivery note, etc.) — skip it
                incoming.processed = True
                incoming.save()
                skipped_count += 1
                logger.info(f"Skipped incoming {incoming.id}: {result.messages[-1] if result.messages else 'not an invoice'}")
            else:
                errors_count += 1
                logger.error(f"Failed to process invoice {incoming.id}: {result.messages}")
                
        except Exception as e:
            errors_count += 1
            logger.error(f"Exception processing invoice {incoming.id}: {str(e)}")
            raise
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            
    return {"task": "process_incoming_invoices", "status": "ok" if not errors_count else "partial", "processed": processed_count, "skipped": skipped_count, "errors": errors_count}

# Manual Upload Pipeline
@shared_task(name="invoices.tasks.process_manual_invoice")
def process_manual_invoice(invoice_id: int):
    """
    Background task to process a single manually uploaded Invoice.
    It uses the same OCR + service as email-based IncomingInvoice processing,
    but starts from the Invoice model instead.
    """
    print(f"Processing manual invoice {invoice_id}")
    invoice = Invoice.objects.get(pk=invoice_id)
    invoice.ocr_processing = True
    invoice.save(update_fields=["ocr_processing"])

    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        logger.error(f"Invoice with id={invoice_id} does not exist.")
        return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": "not found"}

    if not invoice.file:
        logger.error(f"Invoice {invoice_id} has no file attached.")
        # Update status flags
        invoice.ocr_processed = False
        invoice.ocr_processing = False
        invoice.ocr_error = "No file attached to invoice."
        invoice.save(update_fields=["ocr_processed", "ocr_processing", "ocr_error"])
        return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": "no file attached"}


    # Initialize services (same as email task)
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        invoice.ocr_processed = False
        invoice.ocr_processing = False
        invoice.ocr_error = "GEMINI_API_KEY not configured."
        invoice.save(update_fields=["ocr_processed", "ocr_processing", "ocr_error"])
        return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": "missing API key"}
    
    ocr_processor = GeminiInvoiceProcessor(api_key=api_key)
    service = InvoiceService(ocr_processor=ocr_processor)

    temp_file_path = None

    invoice.ocr_processed = False
    invoice.ocr_error = ""
    invoice.save(update_fields=["ocr_processed", "ocr_error"])

    try:
        logger.info(f"Processing MANUAL invoice {invoice.id} (File: {invoice.file.name})")

        # Try to get local path, or download to temp (same pattern as IncomingInvoice)
        try:
            file_path = invoice.file.path
        except NotImplementedError:
            # S3 or other storage without local path access
            suffix = os.path.splitext(invoice.file.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                with invoice.file.open("rb") as f:
                    shutil.copyfileobj(f, tmp)
                temp_file_path = tmp.name
                file_path = temp_file_path

        result = service.process_existing_invoice(invoice, file_path=file_path)

        if result.success:
            logger.info(f"Successfully processed MANUAL invoice {invoice.id}")

            #update status
            invoice.ocr_processing = False
            invoice.ocr_processed = True
            invoice.ocr_error = ""
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
            return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "ok"}
        else:
            if getattr(result, 'is_duplicate', False):
                logger.warning(f"Deleting duplicate MANUAL invoice {invoice.id}")
                invoice_id_for_log = invoice.id
                invoice.delete()
                return {"task": "process_manual_invoice", "invoice_id": invoice_id_for_log, "status": "duplicate_deleted"}

            logger.error(
                f"Failed to process MANUAL invoice {invoice.id}: {result.messages}"
            )
            #processing finished but with error
            invoice.ocr_processing = False
            invoice.ocr_processed = False
            invoice.ocr_error = str(result.messages)
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
            return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": str(result.messages)}

    except Exception as e:
        logger.error(f"Exception processing MANUAL invoice {invoice.id}: {str(e)}")

        # Unexpected exception: also mark as error
        invoice.ocr_processing = False
        invoice.ocr_processed = False
        invoice.ocr_error = str(e)
        invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
        raise  # Re-raise to let Celery/Sentry capture the error

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

