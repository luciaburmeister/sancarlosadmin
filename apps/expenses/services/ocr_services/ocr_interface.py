"""
Contract for any expense ticket OCR provider.
Any implementation (Gemini, Textract, etc.) must subclass TicketOcrInterface.
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict
from dataclasses import dataclass
from decimal import Decimal

from core.services.tax_id_service import normalize_tax_id


@dataclass
class TicketData:
    """Structured data extracted from an expense receipt."""
    vendor_name: Optional[str]
    vendor_tax_id: Optional[str]
    vendor_address: Optional[str]
    vendor_city: Optional[str]
    vendor_country: Optional[str]   # ISO alpha-2 code, e.g. "ES"
    date_of_purchase: Optional[str] # YYYY-MM-DD
    amount: Optional[Decimal]
    currency: Optional[str]         # ISO 4217 code, e.g. "EUR"
    raw_response: Optional[Dict] = None


class TicketOcrInterface(ABC):

    @abstractmethod
    def extract_data(self, file_path: str) -> TicketData:
        """
        Extract expense ticket data from a file (PDF, PNG, JPG).

        Returns:
            TicketData with vendor info, date, and amount.

        Raises:
            ValueError: If the file cannot be processed.
            FileNotFoundError: If the file does not exist.
        """
        raise NotImplementedError

    def normalize_tax_id(self, tax_id: Optional[str]) -> Optional[str]:
        return normalize_tax_id(tax_id)
