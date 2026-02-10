"""
Celery tasks for invoice processing.

This task orchestrates the entire OCR workflow and performs ALL database
operations (saves to Invoice table and creates AppliedTax records).
"""

from celery import shared_task
from django.utils import timezone
import logging
from invoices.services.ocr_services.ocr_interface import OcrInterface
from invoices.models import Invoice, TaxType, AppliedTax
from invoices.invoice_helpers import match_provider
logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0)
def process_invoice_ocr(self, invoice_id: int, ocr_service: OcrInterface):
    """
    Process an invoice file with OCR in the background.
    
    Complete workflow:
    1. Load invoice from database
    2. Extract data using Gemini OCR
    3. Save extracted data to Invoice fields
    4. Match provider and save if confident
    5. Create AppliedTax records for each tax
    6. Mark invoice as processed
    
    ALL database operations happen in this task for clarity and consistency.
    
    Args:
        invoice_id: ID of the Invoice to process
    
    Returns:
        dict: Status information with results
    
    Example:
        >>> from invoices.tasks import process_invoice_ocr
        >>> result = process_invoice_ocr.delay(123)
        >>> result.get()
        {'status': 'success', 'invoice_id': 123, 'taxes_created': 2}
    """

    
    logger.info(f"Starting OCR processing for Invoice ID: {invoice_id}")
    
    try:
        # ============================================
        # STEP 1: GET INVOICE
        # ============================================
        invoice = Invoice.objects.get(id=invoice_id)
        
        # Check if file exists
        if not invoice.file:
            raise ValueError("No file attached to this invoice")
        
        # Mark as processing
        invoice.ocr_processing = True
        invoice.ocr_error = None
        invoice.save(update_fields=['ocr_processing', 'ocr_error'])
        
        # ============================================
        # STEP 2: EXTRACT DATA WITH OCR
        # ============================================

        
        logger.info(f"Calling OCR service for invoice {invoice_id}")
        data = ocr_service.extract_data(invoice.file.path)
        logger.info(f"OCR extraction complete: {data.get('invoice_number')}")
        
        # ============================================
        # STEP 3: SAVE INVOICE FIELDS
        # ============================================
        
        # Basic invoice info
        if data.get('invoice_number'):
            invoice.invoice_number = data['invoice_number']
            logger.info(f"Set invoice_number: {data['invoice_number']}")
        
        if data.get('date_issued'):
            invoice.date_issued = data['date_issued']
            logger.info(f"Set date_issued: {data['date_issued']}")
        
        if data.get('due_date'):
            invoice.due_date = data['due_date']
            logger.info(f"Set due_date: {data['due_date']}")
        
        # Amounts and currency
        if data.get('total_amount'):
            invoice.total_amount = data['total_amount']
            logger.info(f"Set total_amount: {data['total_amount']}")
        
        if data.get('currency'):
            invoice.currency = data['currency']
        
        # Payment method
        if data.get('payment_method'):
            invoice.payment_method = data['payment_method']
        
    
        # ============================================
        # STEP 4: MATCH AND SAVE PROVIDER
        # ============================================
        vendor_name = data.get('vendor_name')
        vendor_tax_id = data.get('vendor_tax_id')
        
        if vendor_name or vendor_tax_id:
            # Call helper to find match (does NOT save)
            provider, confidence, message = match_provider(vendor_name, vendor_tax_id)
            
            logger.info(f"Provider match result: {message} (confidence: {confidence}%)")
            
            # Decision: auto-assign if confidence >= 80%
            if provider and confidence >= 80:
                invoice.provider = provider
                logger.info(f"Auto-assigned provider: {provider.name}")
            else:
                logger.warning(f"Provider not auto-assigned: {message}")
                # Provider field stays None - admin will select manually
        
        # Save all invoice changes to database
        invoice.save()
        logger.info(f"Invoice data saved to database")

        # ============================================
        # STEP 5: SAVE LINE ITEMS TO InvoiceItems
        # ============================================
        
        if data.get('line_items'):
            # Create or update InvoiceItems record
            invoice_items, created = InvoiceItems.objects.get_or_create(
                invoice=invoice
            )
            invoice_items.line_items = data['line_items']
            invoice_items.save()
            
            action = "Created" if created else "Updated"
            logger.info(f"{action} InvoiceItems with {len(data['line_items'])} line items")
        
        # ============================================
        # STEP 6: CREATE TAX RECORDS
        # ============================================
        taxes_created = 0
        taxes_skipped = 0
        
        for order, tax_data in enumerate(data.get('taxes', [])):
            # Extract tax data
            tax_name = tax_data.get('name', '')
            percentage = tax_data.get('percentage')
            base_amount = tax_data.get('base_amount')
            tax_amount = tax_data.get('tax_amount')
            
            # Skip if missing required data
            if not percentage or base_amount is None or tax_amount is None:
                logger.warning(f"Skipping tax - missing data: {tax_data}")
                taxes_skipped += 1
                continue
            
            # Try to find matching TaxType by name and percentage
            tax_type = TaxType.objects.filter(
                tax_name__icontains=tax_name,
                percentage_options__contains=[float(percentage)],
                is_active=True
            ).first()
            
            # If not found by name, try just by percentage
            if not tax_type:
                tax_type = TaxType.objects.filter(
                    percentage_options__contains=[float(percentage)],
                    is_active=True
                ).first()
            
            # Create AppliedTax record if TaxType found
            if tax_type:
                AppliedTax.objects.create(
                    content_object=invoice,
                    tax_type=tax_type,
                    selected_percentage=percentage,
                    base_amount=base_amount,
                    calculated_amount=tax_amount,
                    order=order
                )
                logger.info(
                    f"Created tax: {tax_type.tax_name} {percentage}% "
                    f"(base: {base_amount}, amount: {tax_amount})"
                )
                taxes_created += 1
            else:
                logger.warning(
                    f"Skipping tax - no matching TaxType found for "
                    f"{tax_name} {percentage}%"
                )
                taxes_skipped += 1
        
        logger.info(f"Tax creation complete: {taxes_created} created, {taxes_skipped} skipped")
        
        # ============================================
        # STEP 7: MARK AS SUCCESSFULLY PROCESSED
        # ============================================
        invoice.ocr_processed = True
        invoice.ocr_processing = False
        invoice.ocr_processed_at = timezone.now()
        invoice.save(update_fields=['ocr_processed', 'ocr_processing', 'ocr_processed_at'])
        
        logger.info(f"Successfully processed invoice {invoice_id}")
        
        return {
            'status': 'success',
            'invoice_id': invoice_id,
            'invoice_number': invoice.invoice_number,
            'provider_matched': bool(invoice.provider),
            'taxes_created': taxes_created,
            'taxes_skipped': taxes_skipped,
        }
    
    except Invoice.DoesNotExist:
        error_msg = f"Invoice with ID {invoice_id} does not exist"
        logger.error(error_msg)
        return {'status': 'error', 'message': error_msg}
    
    except Exception as e:
        error_msg = str(e)
        logger.error(
            f"OCR processing failed for invoice {invoice_id}: {error_msg}",
            exc_info=True
        )
        
        # Mark invoice as failed
        try:
            invoice = Invoice.objects.get(id=invoice_id)
            invoice.ocr_processing = False
            invoice.ocr_error = error_msg[:500]  # Truncate if too long
            invoice.save(update_fields=['ocr_processing', 'ocr_error'])
        except:
            pass
        
        return {
            'status': 'error',
            'invoice_id': invoice_id,
            'message': error_msg,
        }