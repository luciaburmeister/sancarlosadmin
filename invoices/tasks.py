from celery import shared_task
from django.conf import settings
from .models import IncomingInvoice, Invoice
from .services.invoice.invoice_service import InvoiceService
from .services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
import logging
import tempfile
import os
import shutil


logger = logging.getLogger(__name__)

#Email Only Pipeline
@shared_task
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
        return "No new invoices"

    logger.info(f"Found {invoices.count()} invoices to process.")
    
    # Initialize services
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        return "Missing API Key"
        
    ocr_processor = GeminiInvoiceProcessor(api_key=api_key)
    service = InvoiceService(ocr_processor=ocr_processor)
    
    processed_count = 0
    errors_count = 0
    
    
    
    for incoming in invoices:
        temp_file_path = None
        try:
            logger.info(f"Processing invoice {incoming.id} (File: {incoming.file.name})")
            
            # Try to get local path, or download to temp
            try:
                file_path = incoming.file.path
            except NotImplementedError:
                # S3 or other storage without local path access
                suffix = os.path.splitext(incoming.file.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    with incoming.file.open('rb') as f:
                        shutil.copyfileobj(f, tmp)
                    temp_file_path = tmp.name
                    file_path = temp_file_path
            
            result = service.process_invoice(file_path)
            
            if result.success:
                incoming.processed = True
                incoming.save()
                processed_count += 1
                logger.info(f"Successfully processed invoice {incoming.id}")
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
            
    return f"Processed {processed_count} invoices, {errors_count} errors"

# Manual Upload Pipeline
@shared_task
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
        return f"Invoice {invoice_id} not found"

    if not invoice.file:
        logger.error(f"Invoice {invoice_id} has no file attached.")
        # Update status flags
        invoice.ocr_processed = False
        invoice.ocr_processing = False
        invoice.ocr_error = "No file attached to invoice."
        invoice.save(update_fields=["ocr_processed", "ocr_processing", "ocr_error"])
        return f"Invoice {invoice_id} has no file"


    # Initialize services (same as email task)
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        invoice.ocr_processed = False
        invoice.ocr_processing = False
        invoice.ocr_error = "GEMINI_API_KEY not configured."
        invoice.save(update_fields=["ocr_processed", "ocr_processing", "ocr_error"])
        return "Missing API Key"
    
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
            return "OK"
        else:
            logger.error(
                f"Failed to process MANUAL invoice {invoice.id}: {result.messages}"
            )
            #processing finished but with error
            invoice.ocr_processing = False
            invoice.ocr_processed = False
            invoice.ocr_error = str(result.messages)
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
            return f"Error: {result.messages}"

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

