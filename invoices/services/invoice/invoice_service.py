"""
Main Invoice Processing Workflow

This service orchestrates the complete invoice processing pipeline:
1. OCR Processing → Extract structured data
2. Provider Matching → Auto-assign or flag for manual review  
3. Invoice Creation → Save to database with items and taxes
4. Result Summary → Return processing status

Input: Invoice file path
Output: Created Invoice instance with processing metadata
"""

from typing import Dict, Tuple, Optional
from decimal import Decimal
from datetime import datetime
from django.db import transaction
from django.core.files import File

from invoices.services.ocr_services import OcrInterface, InvoiceData
from invoices.services.invoice.invoice_service_helpers import match_provider
from invoices.models import Invoice, AppliedTax, TaxType
from materials.models import Provider


class InvoiceProcessingResult:
    """Container for processing results and metadata"""
    
    def __init__(self, invoice: Invoice, success: bool, messages: list, needs_review: bool = False):
        self.invoice = invoice
        self.success = success
        self.messages = messages  # List of processing messages/warnings
        self.needs_review = needs_review  # Provider matching failed or other issues
        
    def __str__(self):
        status = "SUCCESS" if self.success else "FAILED" 
        return f"{status}: Invoice {self.invoice.invoice_number} - {len(self.messages)} messages"


class InvoiceService:
    """
    Main service for processing invoices from upload to database storage.
    
    All processed invoices require manual review regardless of confidence level.
    The confidence percentage helps users understand the reliability of automated matching.
    
    Usage:
        service = InvoiceService(ocr_processor=gemini_processor)
        result = service.process_invoice(file_path="path/to/invoice.pdf")
        
        if result.success:
            print(f"Created invoice: {result.invoice}")
            # result.needs_review is always True
            print("✅ Ready for manual review")
    """
    
    def __init__(self, ocr_processor: OcrInterface):
        """
        Initialize the service with an OCR processor.
        
        Args:
            ocr_processor: Any class implementing OcrInterface (e.g., GeminiInvoiceProcessor)
        """
        self.ocr_processor = ocr_processor
    
    @transaction.atomic  # Ensures all-or-nothing database operations
    def process_invoice(self, file_path: str) -> InvoiceProcessingResult:
        """
        Complete invoice processing workflow.
        
        Args:
            file_path: Path to invoice file
            
        Returns:
            InvoiceProcessingResult with created invoice and processing status
        """
        messages = []
        needs_review = False
        
        try:
            # Step 1: Extract data using OCR
            messages.append("Starting OCR processing...")
            extracted_data = self.ocr_processor.extract_data(file_path)
            messages.append(f"✅ OCR completed - found invoice {extracted_data.invoice_number or 'N/A'}")
            
            provider, confidence, match_message = self._match_provider(extracted_data)
            messages.append(f"Provider matching: {match_message}")
            
            # All invoices need manual review - confidence is just informational
            needs_review = True
            if confidence >= 80:
                messages.append(f"✅ High confidence match ({confidence}%) - but still requires review")
            else:
                messages.append(f"⚠️  Low confidence match ({confidence}%) - requires review")
            
            # Step 3: Create invoice
            invoice = self._create_invoice(extracted_data, provider, file_path)
            messages.append(f"✅ Created invoice: {invoice}")
            
            # Step 4: Create line items
            if extracted_data.line_items:
                self._create_line_items(invoice, extracted_data.line_items)
                messages.append(f"✅ Added {len(extracted_data.line_items)} line items")
            
            # Step 5: Create tax records
            if extracted_data.taxes:
                self._create_applied_taxes(invoice, extracted_data.taxes)
                messages.append(f"✅ Applied {len(extracted_data.taxes)} tax records")
            
            # Step 6: Update OCR status and set manual review flag
            invoice.ocr_processed = True
            invoice.ocr_processing = False
            invoice.ocr_error = None
            invoice.needs_manual_review = True  # Always requires manual review
            invoice.save(update_fields=['ocr_processed', 'ocr_processing', 'ocr_error', 'needs_manual_review'])
            
            return InvoiceProcessingResult(
                invoice=invoice,
                success=True,
                messages=messages,
                needs_review=True  # Always needs review
            )
            
        except Exception as e:
            messages.append(f"❌ Processing failed: {str(e)}")
            
            # Try to create a partial invoice for debugging
            try:
                partial_invoice = self._create_partial_invoice(file_path, str(e))
                return InvoiceProcessingResult(
                    invoice=partial_invoice,
                    success=False,
                    messages=messages,
                    needs_review=True
                )
            except:
                # If we can't even create a partial invoice, re-raise the original error
                raise e
    
    def _match_provider(self, extracted_data: InvoiceData) -> Tuple[Optional[Provider], int, str]:
        """
        Match provider using helper function.
        
        Returns:
            (provider_object, confidence_score, message)
        """
        vendor_name = extracted_data.vendor_name or ''
        vendor_tax_id = extracted_data.vendor_tax_id or ''
        
        return match_provider(vendor_name, vendor_tax_id)
    
    def _create_invoice(self, extracted_data: InvoiceData, provider: Optional[Provider], file_path: str) -> Invoice:
        """
        Create the main Invoice record.
        """
        # Parse dates
        date_issued = self._parse_date(extracted_data.date_issued)
        due_date = self._parse_date(extracted_data.due_date)
        
        # Create invoice
        invoice = Invoice.objects.create(
            invoice_number=extracted_data.invoice_number or 'UNKNOWN',
            date_issued=date_issued or datetime.now().date(),
            due_date=due_date or datetime.now().date(),
            provider=provider,  # Will be None if matching failed
            total_amount=extracted_data.total_amount,
            currency=extracted_data.currency or 'EUR',
            payment_method=extracted_data.payment_method or 'transfer',
            ocr_processing=True  # Mark as currently processing
        )
        
        # Attach the file
        with open(file_path, 'rb') as f:
            invoice.file.save(
                f"invoice_{invoice.invoice_number}.pdf",
                File(f),
                save=True
            )
        
        return invoice
    
    def _create_line_items(self, invoice: Invoice, line_items_data: list):
        """
        Create InvoiceItems record with line items data.
        line_items_data is now a list of LineItem dataclass objects.
        """
        # Convert LineItem dataclasses to dictionaries for JSON storage
        cleaned_items = []
        for item in line_items_data:
            cleaned_items.append({
                'description': item.description or '',
                'quantity': float(item.quantity or 0),
                'unit_price': float(item.unit_price or 0),
                'total': float(item.total or 0),
                'tax_rate': float(item.tax_rate or 0),
            })
        
        InvoiceItems.objects.create(
            invoice=invoice,
            line_items=cleaned_items
        )
    
    def _create_applied_taxes(self, invoice: Invoice, taxes_data: list):
        """
        Create AppliedTax records for the invoice.
        taxes_data is now a list of TaxItem dataclass objects.
        """
        for order, tax_item in enumerate(taxes_data):
            tax_name = tax_item.name or 'VAT'
            percentage = tax_item.percentage or 0
            base_amount = tax_item.base_amount or 0
            tax_amount = tax_item.tax_amount or 0
            
            # Find or create tax type
            tax_type, created = TaxType.objects.get_or_create(
                tax_name=tax_name,
                country='ES',  # Default to Spain
                defaults={
                    'behavior': 'add',
                    'percentage_options': [0, 4, 10, 21],
                    'default_percentage': Decimal(str(percentage)),
                    'is_active': True
                }
            )
            
            # Create applied tax
            AppliedTax.objects.create(
                tax_type=tax_type,
                selected_percentage=Decimal(str(percentage)),
                base_amount=Decimal(str(base_amount)),
                calculated_amount=Decimal(str(tax_amount)),
                content_object=invoice,
                order=order
            )
    
    def _create_partial_invoice(self, file_path: str, error_message: str) -> Invoice:
        """
        Create a minimal invoice record when processing fails.
        Used for debugging and manual processing.
        """
        invoice = Invoice.objects.create(
            invoice_number=f"FAILED_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            date_issued=datetime.now().date(),
            due_date=datetime.now().date(),
            provider=None,
            ocr_processed=False,
            ocr_processing=False,
            ocr_error=error_message,
            needs_manual_review=True  # Failed invoices definitely need review
        )
        
        # Try to attach the file
        try:
            with open(file_path, 'rb') as f:
                invoice.file.save(
                    f"failed_invoice_{invoice.id}.pdf",
                    File(f),
                    save=True
                )
        except:
            pass  # File attachment failed, but invoice is created
        
        return invoice
    
    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime.date]:
        """
        Parse date string to datetime.date object.
        """
        if not date_str:
            return None
        
        try:
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None