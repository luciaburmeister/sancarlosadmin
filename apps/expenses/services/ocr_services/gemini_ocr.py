"""
Gemini implementation of TicketOcrInterface.
Extracts vendor info, date, and amount from an expense receipt.
"""

from typing import Dict, Optional
from decimal import Decimal
from datetime import datetime

from core.currency import normalize_currency
from core.services.gemini_extraction_service import BaseGeminiExtractor, GeminiJSONParseError
from .ocr_interface import TicketOcrInterface, TicketData

__all__ = ["GeminiExpenseProcessor", "GeminiJSONParseError"]


class GeminiExpenseProcessor(BaseGeminiExtractor, TicketOcrInterface):

    MODEL_NAME = "gemini-3-flash-preview"

    SCHEMA = {
        "type": "OBJECT",
        "required": [
            "vendor_name",
            "vendor_tax_id",
            "vendor_address",
            "vendor_city",
            "vendor_country",
            "date_of_purchase",
            "amount",
            "currency",
            "ticket_number",
        ],
        "properties": {
            "vendor_name":    {"type": "STRING", "nullable": True},
            "vendor_tax_id":  {"type": "STRING", "nullable": True},
            "vendor_address": {"type": "STRING", "nullable": True},
            "vendor_city":    {"type": "STRING", "nullable": True},
            "vendor_country": {
                "type": "STRING",
                "nullable": True,
                "description": "ISO 3166-1 alpha-2 country code, e.g. ES, FR, DE",
            },
            "date_of_purchase": {
                "type": "STRING",
                "nullable": True,
                "description": "YYYY-MM-DD",
            },
            "amount": {
                "type": "NUMBER",
                "nullable": True,
                "description": "Total amount paid as printed on the receipt",
            },
            "currency": {
                "type": "STRING",
                "nullable": True,
                "description": "ISO 4217 currency code, e.g. EUR, USD",
            },
            "ticket_number": {
                "type": "STRING",
                "nullable": True,
                "description": (
                    "The receipt's own transaction/ticket/receipt number, distinct from "
                    "the customer or vendor tax ID. Look for labels like 'Trans:', "
                    "'Ticket No.', 'Folio', 'Nº ticket', 'Receipt No.', or the number on "
                    "a simplified invoice. Null if none is printed."
                ),
            },
        },
    }

    PROMPT = """
    Extract information from this expense receipt or ticket.

    Rules:
    - Use null when data is missing or unclear.
    - DATE HANDLING: Receipts may use DD/MM/YYYY or DD/MM/YY (European format).
      Always return dates as YYYY-MM-DD with a 4-digit year.
      The first number is ALWAYS the day, the second is ALWAYS the month.
      A 2-digit year like 26 means 2026, not 1926.
    - vendor_country must be an ISO alpha-2 code (e.g. ES, FR, DE). Infer from
      the address or tax ID format if not printed explicitly.
    - amount is the final total the customer paid (including taxes).
    - currency is the ISO 4217 code. Default to EUR if not stated but context is Spain.
    - vendor_tax_id is the CIF/NIF/VAT number of the shop or vendor, NOT the customer.
    - ticket_number is the receipt's own transaction/ticket/receipt/folio number
      (e.g. "Trans: 1593295"), NOT the vendor's tax ID and NOT the customer's card
      number. Two receipts from the same vendor on the same day for the same
      amount (e.g. two toll charges) will have DIFFERENT ticket numbers — extract
      exactly what is printed, do not invent one.
    """

    def _to_dataclass(self, data: Dict) -> TicketData:
        return TicketData(
            vendor_name=data.get("vendor_name"),
            vendor_tax_id=self.normalize_tax_id(data.get("vendor_tax_id")),
            vendor_address=data.get("vendor_address"),
            vendor_city=data.get("vendor_city"),
            vendor_country=data.get("vendor_country"),
            date_of_purchase=self._parse_date(data.get("date_of_purchase")),
            amount=self._to_decimal(data.get("amount")),
            currency=normalize_currency(data.get("currency")) or "EUR",
            ticket_number=data.get("ticket_number"),
            raw_response=data,
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _parse_date(self, date_str: Optional[str]) -> Optional[str]:
        if not date_str:
            return None

        try:
            parsed = datetime.strptime(date_str, "%Y-%m-%d")
            if parsed.year >= 2000:
                return date_str
        except ValueError:
            pass

        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                parsed = datetime.strptime(date_str, fmt)
                if parsed.year > 2000:
                    return parsed.strftime("%Y-%m-%d")
            except ValueError:
                continue

        for fmt in ("%d/%m/%y", "%d-%m-%y", "%d.%m.%y"):
            try:
                return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

        return None

    def _to_decimal(self, value) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None
