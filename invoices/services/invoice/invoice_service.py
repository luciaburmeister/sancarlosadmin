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
from djmoney.money import Money


from invoices.services import invoice
from invoices.services.ocr_services import OcrInterface, InvoiceData, GeminiJSONParseError
from invoices.models import Invoice, AppliedTax, TaxType, InvoiceItem
from materials.models import Provider
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from invoices.services.invoice.invoice_duplicate_service import InvoiceDuplicateService, DuplicateInvoiceError
from company.models import Company
from core.services.provider_service import get_or_create_provider




class InvoiceProcessingResult:
    """Container for processing results and metadata"""
    
    def __init__(self, invoice: Optional[Invoice], success: bool, messages: list, needs_review: bool = False, is_duplicate: bool = False, duplicate_of: Optional[Invoice] = None):
        self.invoice = invoice
        self.success = success
        self.messages = messages  # List of processing messages/warnings
        self.needs_review = needs_review  # Provider matching failed or other issues
        self.is_duplicate = is_duplicate
        self.duplicate_of = duplicate_of  # The existing invoice this one conflicts with, if any
        
    def __str__(self):
        status = "SUCCESS" if self.success else "FAILED"
        inv = self.invoice.invoice_number if self.invoice else "N/A"
        return f"{status}: Invoice {inv} - {len(self.messages)} messages"


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
    
    def process_invoice(self, file_path: str, sender_email: Optional[str] = None) -> InvoiceProcessingResult:
        """
        Complete invoice processing workflow.
        
        Args:
            file_path: Path to invoice file
            
        Returns:
            InvoiceProcessingResult with created invoice and processing status
        """
        messages = []
        
        try:
            # Step 1: Extract data using OCR (Outside atomic to keep things clean)
            messages.append("Checking for duplicate invoices...")
            duplicate_result = self.duplicate_service.check_for_duplicate(file_path)
            
            if duplicate_result.is_duplicate:
                raise DuplicateInvoiceError(
                    duplicate_result.similar_object,
                    duplicate_result.similarity_distance
                )
            messages.append("No duplicates found - proceeding with processing")

            messages.append("Starting OCR processing...")
            extracted_data = self.ocr_processor.extract_data(file_path)
            messages.append(f"OCR completed - found invoice {extracted_data.invoice_number or 'N/A'}")

            # Skip non-invoice documents (quotes, delivery notes, etc.)
            if extracted_data.document_type not in ("invoice", "credit_note"):
                messages.append(
                    f"Document classified as '{extracted_data.document_type}', not an invoice — skipping."
                )
                return InvoiceProcessingResult(
                    invoice=None,
                    success=False,
                    messages=messages,
                    needs_review=False,
                )

            # Step 2: Atomic DB Operations
            with transaction.atomic():
                # Match company and provider
                company = self._find_company(extracted_data.company_tax_id)
                provider = get_or_create_provider(
                    extracted_data.vendor_tax_id,
                    extracted_data.vendor_name,
                    company,
                    sender_email
                )
                if not provider:
                    messages.append("No tax ID extracted — provider not linked, needs manual review.")

                # Check for existing invoice by number/provider (metadata duplicate)
                invoice_number = extracted_data.invoice_number or 'UNKNOWN'
                if provider and invoice_number != 'UNKNOWN':
                    existing = Invoice.objects.filter(provider=provider, invoice_number=invoice_number).first()
                    if existing:
                        return InvoiceProcessingResult(
                            invoice=existing,
                            success=True,
                            messages=messages + ["Duplicate detected: invoice with same number already exists for this provider."],
                            needs_review=True,
                            is_duplicate=True
                        )
                
                # Create invoice with hash
                invoice = self._create_invoice(extracted_data, provider, file_path, duplicate_result.hash_value, company)
                messages.append(f"Created invoice: {invoice}")
                
                # Create line items
                if extracted_data.line_items:
                    self._create_line_items(invoice, extracted_data.line_items)
                    messages.append(f"Added {len(extracted_data.line_items)} line items")
                
                # Create tax records
                if extracted_data.taxes:
                    self._create_applied_taxes(invoice, extracted_data.taxes)
                    messages.append(f"Applied {len(extracted_data.taxes)} tax records")
                
                # Mark as processed — use update_fields to preserve OCR totals
                invoice.ocr_processed = True
                invoice.ocr_processing = False
                invoice.ocr_error = None
                invoice.needs_manual_review = True
                invoice.save(update_fields=[
                    'ocr_processed', 'ocr_processing', 'ocr_error', 'needs_manual_review',
                ])
                invoice.verify_total_balance()
            
            return InvoiceProcessingResult(
                invoice=invoice,
                success=True,
                messages=messages,
                needs_review=True
            )
        
        except DuplicateInvoiceError as e: 
            messages.append(f"Duplicate detected (file hash): {str(e)}")
            return InvoiceProcessingResult(
                invoice=e.similar_invoice,  # Return the existing invoice
                success=True,
                messages=messages,
                needs_review=True,
                is_duplicate=True
            )
        
        except Exception as e:
            messages.append(f"Processing failed: {str(e)}")

            # Try to create a partial invoice for debugging
            try:
                raw_response = e.raw_text if isinstance(e, GeminiJSONParseError) else None
                partial_invoice = self._create_partial_invoice(file_path, str(e), raw_response=raw_response)
                return InvoiceProcessingResult(
                    invoice=partial_invoice,
                    success=False,
                    messages=messages,
                    needs_review=True
                )
            except Exception:
                # If we can't even create a partial invoice, re-raise the original error
                raise e
    
    def process_existing_invoice(self, invoice: Invoice, file_path: Optional[str] = None) -> InvoiceProcessingResult:
        """
        Pipeline B: OCR + UPDATE the existing invoice.
        IMPORTANT: Do NOT create a new invoice. Do NOT re-save the file.
        """
        messages = []
        try:
            # 1. Initial status update (Outside atomic to ensure it persists)
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

            # 2. OCR (Non-DB)
            extracted_data = self.ocr_processor.extract_data(file_path)
            
            # 3. Atomic DB Operations
            with transaction.atomic():
                # Match company and provider
                company = self._find_company(extracted_data.company_tax_id)
                provider = get_or_create_provider(
                    extracted_data.vendor_tax_id,
                    extracted_data.vendor_name,
                    company
                )
                if not provider:
                    messages.append("No tax ID extracted — provider not linked, needs manual review.")

                invoice_num = extracted_data.invoice_number or invoice.invoice_number or "UNKNOWN"
                
                # Check for conflict with ANOTHER invoice (not itself), scoped to the same company
                if company and provider and invoice_num != 'UNKNOWN':
                    conflict = Invoice.objects.filter(company=company, provider=provider, invoice_number=invoice_num).exclude(pk=invoice.pk).first()
                    if conflict:
                        return InvoiceProcessingResult(
                            invoice=invoice,
                            success=False,
                            messages=messages + [f"Conflict detected: another invoice ({conflict.id}) already has number {invoice_num} for this provider and company."],
                            needs_review=True,
                            is_duplicate=True,
                            duplicate_of=conflict,
                        )

                invoice.invoice_number = invoice_num
                invoice.date_issued = self._parse_date(extracted_data.date_issued) or invoice.date_issued or timezone.now().date()
                invoice.due_date = self._parse_date(extracted_data.due_date) or invoice.due_date or invoice.date_issued
                invoice.accounting_date = datetime.now().date()
                invoice.provider = provider
                currency = extracted_data.currency or invoice.currency or "EUR"
                invoice.currency = currency
                if extracted_data.net_amount is not None:
                    invoice.net_amount = Money(extracted_data.net_amount, currency)
                elif invoice.net_amount is not None:
                    invoice.net_amount = Money(invoice.net_amount.amount, currency)

                if extracted_data.total_amount is not None:
                    invoice.total_amount = Money(extracted_data.total_amount, currency)
                elif invoice.total_amount is not None:
                    invoice.total_amount = Money(invoice.total_amount.amount, currency)
                invoice.payment_method = extracted_data.payment_method or invoice.payment_method or "transfer"
                invoice.company = company
                invoice.ocr_raw_response = extracted_data.raw_response
                if extracted_data.document_type in ("invoice", "credit_note"):
                    invoice.document_type = extracted_data.document_type
                invoice.needs_manual_review = True

                # Replace items
                if extracted_data.line_items:
                    invoice.items.all().delete()
                    self._create_line_items(invoice, extracted_data.line_items)

                # Replace taxes
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
                invoice.save(update_fields=[
                    'invoice_number', 'date_issued', 'due_date', 'accounting_date', 'provider',
                    'net_amount', 'net_amount_currency',
                    'total_amount', 'total_amount_currency',
                    'currency', 'payment_method', 'company', 'ocr_raw_response',
                    'document_type',
                    'needs_manual_review', 'ocr_processing', 'ocr_processed', 'ocr_error',
                ])
                invoice.verify_total_balance()

            return InvoiceProcessingResult(invoice=invoice, success=True, messages=messages, needs_review=True)

        except Exception as e:
            messages.append(f"Processing failed: {str(e)}")
            invoice.ocr_processing = False
            invoice.ocr_processed = False
            invoice.ocr_error = str(e)
            if isinstance(e, GeminiJSONParseError):
                invoice.ocr_raw_response = e.raw_text
            invoice.needs_manual_review = True
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error", "ocr_raw_response", "needs_manual_review"])
            raise  # Re-raise to let Sentry capture the error
    
    
    def _create_invoice(self, extracted_data: InvoiceData, provider: Optional[Provider], file_path: str,image_hash: str, company:Company) -> Invoice:
        """
        Create the main Invoice record with image hash
        """
        # Parse dates
        date_issued = self._parse_date(extracted_data.date_issued) or datetime.now().date()
        due_date = self._parse_date(extracted_data.due_date)

        currency = extracted_data.currency or 'EUR'
        net_money = Money(extracted_data.net_amount, currency) if extracted_data.net_amount is not None else None
        total_money = Money(extracted_data.total_amount, currency) if extracted_data.total_amount is not None else None

        # Create invoice
        invoice = Invoice.objects.create(
            invoice_number=extracted_data.invoice_number or 'UNKNOWN',
            date_issued=date_issued,
            due_date=due_date or date_issued,
            accounting_date=datetime.now().date(),
            provider=provider,  # Will be None if matching failed
            net_amount=net_money,
            total_amount=total_money,
            currency=currency,
            payment_method=extracted_data.payment_method or 'transfer',
            image_hash=image_hash,
            company=company,
            ocr_raw_response=extracted_data.raw_response,
            document_type=(
                extracted_data.document_type
                if extracted_data.document_type in ("invoice", "credit_note")
                else "invoice"
            ),
            ocr_processing=True  # Mark as currently processing
        )
        
        # Attach the file (save=False to avoid triggering recalculate_totals
        # which would overwrite total_amount to 0 since items don't exist yet)
        with open(file_path, 'rb') as f:
            invoice.file.save(
                f"invoice_{invoice.invoice_number}.pdf",
                File(f),
                save=False,
            )
            invoice.save(update_fields=["file"])
        
        return invoice
    
    def _create_line_items(self, invoice: Invoice, line_items_data: list):
        currency = invoice.currency or 'EUR'
        for item in line_items_data:
            unit_price_val = item.unit_price or item.net_total or Decimal('0')
            InvoiceItem.objects.create(
                invoice=invoice,
                description=(item.description or '')[:255],
                quantity=item.quantity or Decimal('1'),
                unit_price=Money(unit_price_val, currency),
                discount_percentage=item.discount_percentage or Decimal('0'),
            )
        
    def _create_applied_taxes(self, invoice: Invoice, taxes_data: list):
        """
        Create AppliedTax records for the invoice.
        taxes_data is now a list of TaxItem dataclass objects.
        Consolidates taxes by (tax_type, percentage) to avoid unique constraint violations.
        """
        consolidated_taxes = {} # key: (tax_type_id, percentage), value: dict with sum

        for order, tax_item in enumerate(taxes_data):
            raw_name = tax_item.name or 'IVA'
            # Normalize using the centralized model logic
            tax_name = TaxType.normalize_tax_name(raw_name)
            if not tax_name:
                tax_name = 'IVA'

            # Normalize values
            percentage = Decimal(str(abs(tax_item.percentage or 0)))
            base_amount = Decimal(str(tax_item.base_amount or 0))
            tax_amount = Decimal(str(tax_item.tax_amount or 0))
            
            # Find or create tax type to get a consistent ID for grouping
            behavior = 'subtract' if (tax_amount < 0 or 'IRPF' in tax_name) else 'add'
            is_debit = (behavior == 'add')
            
            tax_type, created = TaxType.objects.get_or_create(
                tax_name=tax_name,
                country='ES',  # Default to Spain
                defaults={
                    'behavior': behavior,
                    'is_debit': is_debit,
                    'percentage_options': [0, 4, 10, 21],
                    'default_percentage': percentage,
                    'is_active': True
                }
            )
            
            key = (tax_type.id, percentage)
            if key not in consolidated_taxes:
                consolidated_taxes[key] = {
                    'tax_type': tax_type,
                    'percentage': percentage,
                    'base_amount': Decimal('0'),
                    'tax_amount': Decimal('0'),
                    'order': order
                }
            
            consolidated_taxes[key]['base_amount'] += base_amount
            consolidated_taxes[key]['tax_amount'] += tax_amount

        # Create applied tax records from the consolidated results
        currency = invoice.currency or 'EUR'
        for data in consolidated_taxes.values():
            AppliedTax.objects.create(
                tax_type=data['tax_type'],
                selected_percentage=data['percentage'],
                base_amount=Money(data['base_amount'], currency),
                calculated_amount=Money(data['tax_amount'], currency),
                content_object=invoice,
                order=data['order']
            )
    
    def _create_partial_invoice(self, file_path: str, error_message: str, raw_response: Optional[str] = None) -> Invoice:
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
            ocr_raw_response=raw_response,
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
        clean_tax_id = self.ocr_processor.normalize_tax_id(tax_id)
        if not clean_tax_id:
            return None
        return Company.objects.filter(tax_identifier__iexact=clean_tax_id).first()