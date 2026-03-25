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

from typing import Optional
from decimal import Decimal
from datetime import datetime
from django.db import transaction
from django.core.files import File


from invoices.services import invoice
from invoices.services.ocr_services import OcrInterface, InvoiceData
from invoices.models import Invoice, AppliedTax, TaxType, InvoiceItem
from materials.models import Provider
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from invoices.services.invoice.invoice_duplicate_service import InvoiceDuplicateService, DuplicateInvoiceError
from company.models import Company




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
            print("Ready for manual review")
    """
    
    def __init__(self, ocr_processor: OcrInterface):
        """
        Initialize the service with an OCR processor + duplicate detection service.
        """
        self.ocr_processor = ocr_processor
        self.duplicate_service = InvoiceDuplicateService() 
    
    @transaction.atomic  # Ensures all-or-nothing database operations
    def process_invoice(self, file_path: str, sender_email: Optional[str] = None) -> InvoiceProcessingResult:
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
            # Step 0: Check for duplicates FIRST
            messages.append("Checking for duplicate invoices...")
            duplicate_result = self.duplicate_service.check_for_duplicate(file_path)
            
            if duplicate_result.is_duplicate:
                raise DuplicateInvoiceError(
                    duplicate_result.similar_invoice, 
                    duplicate_result.similarity_distance
                )
            messages.append("No duplicates found - proceeding with processing")

            # Step 1: Extract data using OCR
            messages.append("Starting OCR processing...")
            extracted_data = self.ocr_processor.extract_data(file_path)
            messages.append(f"OCR completed - found invoice {extracted_data.invoice_number or 'N/A'}")
            
            # Step 2: Match company and provider
            company = self._find_company(extracted_data.company_tax_id)
            provider = self._get_or_create_provider(
                extracted_data.vendor_tax_id, 
                extracted_data.vendor_name, 
                company,
                sender_email
            )
            
            # Step 3: Create invoice with hash
            invoice = self._create_invoice(extracted_data, provider, file_path, duplicate_result.hash_value, company)
            messages.append(f"Created invoice: {invoice}")
            
            # Step 4: Create line items
            if extracted_data.line_items:
                self._create_line_items(invoice, extracted_data.line_items)
                messages.append(f"Added {len(extracted_data.line_items)} line items")
            
            # Step 5: Create tax records
            if extracted_data.taxes:
                self._create_applied_taxes(invoice, extracted_data.taxes)
                messages.append(f"Applied {len(extracted_data.taxes)} tax records")
            
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
        
        except DuplicateInvoiceError as e: 
            messages.append(f"Duplicate detected: {str(e)}")
            return InvoiceProcessingResult(
                invoice=e.similar_invoice,  # Return the existing invoice
                success=False,
                messages=messages,
                needs_review=True
            )
        
        except Exception as e:
            messages.append(f"Processing failed: {str(e)}")
            
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
            finally:
                # Re-raise the original exception after attempt to create partial invoice
                raise e
    
    @transaction.atomic
    def process_existing_invoice(self, invoice: Invoice, file_path: Optional[str] = None) -> InvoiceProcessingResult:
        """
        Pipeline B: OCR + UPDATE the existing invoice.
        IMPORTANT: Do NOT create a new invoice. Do NOT re-save the file.
        """
        messages = []
        try:
            # mark processing
            invoice.ocr_processing = True
            invoice.ocr_processed = False
            invoice.ocr_error = ""
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])

            if not invoice.file:
                raise ValueError("No file attached to invoice.")

            if file_path is None:
                try:
                    file_path = invoice.file.path
                except Exception:
                    raise ValueError("Invoice file has no local path. Provide file_path from the task.")

            
            extracted_data = self.ocr_processor.extract_data(file_path)
            
            #If no tax id found, how are we gonna throw error
            # Step 2: Match company and provider
            company = self._find_company(extracted_data.company_tax_id)
            provider = self._get_or_create_provider(
                extracted_data.vendor_tax_id, 
                extracted_data.vendor_name, 
                company
            )


            invoice.invoice_number = extracted_data.invoice_number or invoice.invoice_number or "UNKNOWN"
            invoice.date_issued = self._parse_date(extracted_data.date_issued) or invoice.date_issued or timezone.now().date()
            invoice.due_date = self._parse_date(extracted_data.due_date) or invoice.due_date or timezone.now().date()
            invoice.provider = provider
            invoice.total_amount = extracted_data.total_amount or invoice.total_amount
            invoice.currency = extracted_data.currency or invoice.currency or "EUR"
            invoice.payment_method = extracted_data.payment_method or invoice.payment_method or "transfer"
            invoice.company = company
            invoice.needs_manual_review = True  # always True initially

            if extracted_data.line_items:
                invoice.items.all().delete()
                self._create_line_items(invoice, extracted_data.line_items)

            invoice.save()  # one save for all updated fields

            if extracted_data.taxes:
                AppliedTax.objects.filter(
                    content_type=ContentType.objects.get_for_model(invoice),
                    object_id=invoice.pk,
                ).delete()

                self._create_applied_taxes(invoice, extracted_data.taxes)

            invoice.ocr_processing = False
            invoice.ocr_processed = True
            invoice.ocr_error = None
            invoice.needs_manual_review = True
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error", "needs_manual_review"])

            return InvoiceProcessingResult(invoice=invoice, success=True, messages=messages, needs_review=True)

        except Exception as e:
            messages.append(f"Processing failed: {str(e)}")
            invoice.ocr_processing = False
            invoice.ocr_processed = False
            invoice.ocr_error = str(e)
            invoice.needs_manual_review = True
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error", "needs_manual_review"])
            raise  # Re-raise to let Sentry capture the error
    
    
    def _create_invoice(self, extracted_data: InvoiceData, provider: Optional[Provider], file_path: str,image_hash: str, company:Company) -> Invoice:
        """
        Create the main Invoice record with image hash
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
            image_hash=image_hash,
            company=company,
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
        for item in line_items_data:
            InvoiceItem.objects.create(
                invoice=invoice,
                description=item.description or '',
                quantity=item.quantity or Decimal('1'),
                unit_price=item.unit_price or Decimal('0'),
            )
        
    def _create_applied_taxes(self, invoice: Invoice, taxes_data: list):
        """
        Create AppliedTax records for the invoice.
        taxes_data is now a list of TaxItem dataclass objects.
        """
        for order, tax_item in enumerate(taxes_data):
            raw_name = tax_item.name or 'VAT'
            # Normalize: remove non-letters, uppercase
            tax_name = ''.join(filter(str.isalpha, raw_name)).upper()  
            if not tax_name:                                           
                tax_name = 'VAT'                                        

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

    def _find_company(self, tax_id: Optional[str]) -> Optional[Company]:
        """Lookup company by tax ID"""
        if not tax_id:
            return None
        return Company.objects.filter(tax_identifier__iexact=tax_id.strip()).first()

    def _get_or_create_provider(self, vendor_tax_id: Optional[str], vendor_name: Optional[str], company: Optional[Company], sender_email: Optional[str] = None) -> Optional[Provider]:
        """
        Lookup provider by email or tax ID and company.
        If not found, create a new one with the extracted vendor details.
        """
        if sender_email and company:
            provider_by_email = Provider.objects.filter(
                associated_emails__contains=[sender_email],
                company=company
            ).first()
            if provider_by_email:
                return provider_by_email

        if not vendor_tax_id:
            return None

        clean_tax_id = vendor_tax_id.strip()
        
        provider = Provider.objects.filter(
            tax_identifier__iexact=clean_tax_id,
            company=company
        ).first()

        if not provider and company:
            provider = Provider.objects.create(
                name=vendor_name or f"New Provider {clean_tax_id}",
                tax_identifier=clean_tax_id,
                company=company
            )
            
        return provider