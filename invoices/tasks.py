from celery import shared_task
from django.conf import settings
from .models import IncomingInvoice
from .services.invoice.invoice_service import InvoiceService
from .services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
import logging
import tempfile
import os
import shutil


logger = logging.getLogger(__name__)

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
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            
    return f"Processed {processed_count} invoices, {errors_count} errors"
