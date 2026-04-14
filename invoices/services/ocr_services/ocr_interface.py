"""
Invoice logic regrardless of the OCR provider used

A "contract" or "rulebook" NOT a workflow
"""

"""
Invoice OCR interface - defines the contract for any OCR provider.

This is a "contract" or "rulebook" NOT a workflow.
Any OCR service (Gemini, AWS Textract, etc.) must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from dataclasses import dataclass
from decimal import Decimal

@dataclass
class LineItem:
    """Represents a single line item on an invoice"""
    description: Optional[str]
    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    discount_percentage: Optional[Decimal]
    gross_total: Optional[Decimal]  # before discount
    net_total: Optional[Decimal]    # after discount
    tax_rate: Optional[Decimal]

@dataclass 
class TaxItem:
    """Represents a tax applied to an invoice"""
    name: Optional[str]
    percentage: Optional[Decimal]
    base_amount: Optional[Decimal]
    tax_amount: Optional[Decimal]

@dataclass
class InvoiceData:
    """Structured invoice data extracted from OCR"""
    vendor_name: Optional[str]
    vendor_tax_id: Optional[str]
    company_tax_id: Optional[str]
    invoice_number: Optional[str]
    date_issued: Optional[str]  # YYYY-MM-DD format
    due_date: Optional[str]     # YYYY-MM-DD format
    gross_amount: Optional[Decimal]   # invoice total before discounts
    total_amount: Optional[Decimal]
    currency: Optional[str]
    payment_method: Optional[str]
    line_items: List[LineItem]
    taxes: List[TaxItem]


class OcrInterface(ABC):
    
    @abstractmethod
    def extract_data(self, file_path: str) -> InvoiceData:
        """
        Extract invoice data from a file.
        
        Args:
            file_path (str): Full path to the invoice file (PDF, PNG, JPG)
            
        Returns:
            InvoiceData: Structured dataclass with extracted invoice information:
            - vendor_name, vendor_tax_id
            - invoice_number, dates
            - total_amount, currency, payment_method  
            - line_items: List[LineItem]
            - taxes: List[TaxItem]
            
        Raises:
            ValueError: If file cannot be processed or data is invalid
            FileNotFoundError: If file doesn't exist
        """
        raise NotImplementedError("Subclasses must implement extract_data method")

    def normalize_tax_id(self, tax_id: Optional[str]) -> Optional[str]:
        """
        Normalize tax ID by removing non-alphanumeric characters and converting to uppercase.
        Also removes 'ES' prefix if present.
        """
        if tax_id is None:
            return None
            
        # Remove non-alphanumeric and uppercase
        clean_tax_id = "".join(filter(str.isalnum, tax_id)).upper()
        
        # Remove ES prefix if present (Spanish country code)
        if clean_tax_id.startswith("ES"):
            clean_tax_id = clean_tax_id[2:]
            
        return clean_tax_id
