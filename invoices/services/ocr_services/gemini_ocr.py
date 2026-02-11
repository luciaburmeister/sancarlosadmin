"""
Maps raw responses to extracted_dataclass structure
"""

import google.genai as genai
from google.genai import types
import json
from typing import Dict, Optional
from decimal import Decimal
from datetime import datetime

from .ocr_interface import OcrInterface, InvoiceData, LineItem, TaxItem




class GeminiInvoiceProcessor(OcrInterface):

    INVOICE_SCHEMA = {
    "type": "OBJECT",
    "required": [
        "vendor_name",
        "vendor_tax_id",
        "invoice_number",
        "date_issued",
        "due_date",
        "total_amount",
        "currency",
        "payment_method",
        "line_items",
        "taxes",
    ],
    "properties": {
        "vendor_name": {"type": "STRING", "nullable": True},
        "vendor_tax_id": {"type": "STRING", "nullable": True},
        "invoice_number": {"type": "STRING", "nullable": True},

        "date_issued": {
            "type": "STRING",
            "nullable": True,
            "description": "YYYY-MM-DD",
        },
        "due_date": {
            "type": "STRING",
            "nullable": True,
            "description": "YYYY-MM-DD",
        },

        "total_amount": {"type": "NUMBER", "nullable": True},
        "currency": {"type": "STRING", "nullable": True},

        "payment_method": {
            "type": "STRING",
            "nullable": True,
            "enum": ["transfer", "cash", "card"],
        },

        "line_items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": [
                    "description",
                    "quantity",
                    "unit_price",
                    "total",
                    "tax_rate",
                ],
                "properties": {
                    "description": {"type": "STRING", "nullable": True},
                    "quantity": {"type": "NUMBER", "nullable": True},
                    "unit_price": {"type": "NUMBER", "nullable": True},
                    "total": {"type": "NUMBER", "nullable": True},
                    "tax_rate": {"type": "NUMBER", "nullable": True},
                },
            },
        },

        "taxes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "required": [
                    "name",
                    "percentage",
                    "base_amount",
                    "tax_amount",
                ],
                "properties": {
                    "name": {"type": "STRING", "nullable": True},
                    "percentage": {"type": "NUMBER", "nullable": True},
                    "base_amount": {"type": "NUMBER", "nullable": True},
                    "tax_amount": {"type": "NUMBER", "nullable": True},
                },
            },
        },
    },
}


    def __init__(self, api_key: str):
        """
        Initialize the processor.
        """
        self.client = genai.Client(api_key=api_key)
        self.model_name = "gemini-flash-latest"

    def extract_data(self, file_path: str) -> InvoiceData:
        """
        Extract invoice data from a file (PDF, PNG, JPG).
        """

        mime_type = self._get_mime_type(file_path)

        uploaded_file = self.client.files.upload(
            file=file_path,
        )

        print(f"File uploaded: {uploaded_file.uri}")

        prompt = """
        Extract ALL information from this invoice.

        Rules:
        - Use null when data is missing
        - Dates must be YYYY-MM-DD
        - Numbers must be numeric
        - payment_method must be: transfer, cash, or card
        - line_items MUST include tax_rate
        - taxes MUST include base_amount and tax_amount
        - The vendor_tax_id has to be the provider of who is issuing the invoice not the client's one.
        """

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[
                uploaded_file,
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=self.INVOICE_SCHEMA,
                temperature=0.0,
            ),
        )
        part = response.candidates[0].content.parts[0]

        raw = json.loads(part.json())["text"]

        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, dict):
            data = raw
        else:
            raise TypeError(f"Unexpected Gemini JSON type: {type(raw)}")

        return self._convert_to_dataclass(data)

    # =========================
    # CONVERSION
    # =========================

    def _convert_to_dataclass(self, data: Dict) -> InvoiceData:
        """
        Convert raw dictionary to InvoiceData dataclass.
        """

        line_items = [
            LineItem(
                description=item.get("description"),
                quantity=self._to_decimal(item.get("quantity")),
                unit_price=self._to_decimal(item.get("unit_price")),
                total=self._to_decimal(item.get("total")),
                tax_rate=self._to_decimal(item.get("tax_rate")),
            )
            for item in data.get("line_items", [])
        ]

        taxes = [
            TaxItem(
                name=tax.get("name"),
                percentage=self._to_decimal(tax.get("percentage")),
                base_amount=self._to_decimal(tax.get("base_amount")),
                tax_amount=self._to_decimal(tax.get("tax_amount")),
            )
            for tax in data.get("taxes", [])
        ]
        return InvoiceData(
            vendor_name=data.get("vendor_name"),
            vendor_tax_id=data.get("vendor_tax_id"),
            invoice_number=data.get("invoice_number"),
            date_issued=self._parse_date(data.get("date_issued")),
            due_date=self._parse_date(data.get("due_date")),
            total_amount=self._to_decimal(data.get("total_amount")),
            currency=data.get("currency", "EUR"),
            payment_method=self._normalize_payment_method(
                data.get("payment_method")
            ),
            line_items=line_items,
            taxes=taxes,
        )

    # =========================
    # HELPERS
    # =========================

    def _get_mime_type(self, file_path: str) -> str:
        ext = file_path.lower().split(".")[-1]
        return {
            "pdf": "application/pdf",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
        }.get(ext, "application/pdf")

    def _normalize_payment_method(self, method: Optional[str]) -> Optional[str]:
        if not method:
            return None
        m = method.lower()
        if any(k in m for k in ("transfer", "bank", "bancaria")):
            return "transfer"
        if any(k in m for k in ("cash", "efectivo", "contado")):
            return "cash"
        if any(k in m for k in ("card", "tarjeta", "credit")):
            return "card"
        return None

    def _parse_date(self, date_str: Optional[str]) -> Optional[str]:
        if not date_str:
            return None
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return date_str
        except ValueError:
            return None

    def _to_decimal(self, value) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None
