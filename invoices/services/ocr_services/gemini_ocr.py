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
        "company_tax_id",
        "invoice_number",
        "date_issued",
        "due_date",
        "net_amount",
        "total_amount",
        "currency",
        "payment_method",
        "line_items",
        "taxes",
    ],
    "properties": {
        "vendor_name": {"type": "STRING", "nullable": True},
        "vendor_tax_id": {"type": "STRING", "nullable": True},
        "company_tax_id": {"type": "STRING", "nullable": True},
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
        "net_amount": {
            "type": "NUMBER",
            "nullable": True,
            "description": "Invoice subtotal before taxes (Base Imponible)"
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
                    "discount_percentage",
                    "gross_total",
                    "net_total",
                    "tax_rate",
                ],
                "properties": {
                    "description": {"type": "STRING", "nullable": True},
                    "quantity": {"type": "NUMBER", "nullable": True},
                    "unit_price": {"type": "NUMBER", "nullable": True},
                    "discount_percentage": {
                        "type": "NUMBER",
                        "nullable": True,
                        "description": "Discount as a percentage, e.g. 30 for 30%"
                    },
                    "gross_total": {
                        "type": "NUMBER",
                        "nullable": True,
                        "description": "Line total before discount (quantity x unit_price)"
                    },
                    "net_total": {
                        "type": "NUMBER",
                        "nullable": True,
                        "description": "Line total after discount applied"
                    },
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
                    "name": {"type": "STRING", "nullable": True, "enum": ["IVA", "IRPF", "RETENCION"]},
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
        - taxes names MUST be classified into: IVA, IRPF, or RETENCION.
        - The vendor_tax_id has to be the provider of who is issuing the invoice not the client's one.
        - company_tax_id = tax id of the customer/client receiving the invoice (us)
        - Do NOT confuse vendor_tax_id with company_tax_id

      CRITICAL - Amounts on credit notes (any invoice where amounts are negative, regardless of language):
        - A credit note is any invoice whose monetary amounts are negative.
        - Do NOT calculate net_amount or total_amount. Read both values directly from the document.
        - Do NOT adjust, correct, or apply taxes to derive either value. Just read what is printed.
        - On a credit note, taxes make total_amount MORE negative than net_amount, not less.
        - Therefore: total_amount is always more negative than net_amount (further from zero).
        - Wrong reasoning: net=-X, tax=Y, so total=-X+Y. THIS IS INCORRECT.
        - Correct reasoning: net=-X, tax=-Y, so total=-X+(-Y)=-X-Y. THIS IS CORRECT.

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

        invoice_data = self._convert_to_dataclass(data, raw_response=data)
        return self._adjust_tax_inclusive_prices(invoice_data)

    # =========================
    # CONVERSION
    # =========================

    def _convert_to_dataclass(self, data: Dict, raw_response: Optional[Dict] = None) -> InvoiceData:
        """
        Convert raw dictionary to InvoiceData dataclass.
        """

        line_items = [
            LineItem(
                description=item.get("description"),
                quantity=self._to_decimal(item.get("quantity")),
                unit_price=self._to_decimal(item.get("unit_price")),
                discount_percentage=self._to_decimal(item.get("discount_percentage")),
                gross_total=self._to_decimal(item.get("gross_total")),
                net_total=self._to_decimal(item.get("net_total")),
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
            vendor_tax_id=self.normalize_tax_id(data.get("vendor_tax_id")),
            company_tax_id=self.normalize_tax_id(data.get("company_tax_id")),
            invoice_number=data.get("invoice_number"),
            date_issued=self._parse_date(data.get("date_issued")),
            due_date=self._parse_date(data.get("due_date")),
            net_amount=self._to_decimal(data.get("net_amount")),
            total_amount=self._to_decimal(data.get("total_amount")),
            currency=data.get("currency", "EUR"),
            payment_method=self._normalize_payment_method(
                data.get("payment_method")
            ),
            line_items=line_items,
            taxes=taxes,
            raw_response=raw_response,
        )

    def _adjust_tax_inclusive_prices(self, data: InvoiceData) -> InvoiceData:
        """
        Detects if line item prices include tax (IVA) and adjusts them to net prices.

        Detection: If sum of line item net_totals ≈ total_amount (not net_amount),
        the prices are tax-inclusive and need to be divided by (1 + tax_rate/100).

        Example: BAUHAUS receipt shows unit_price=10.95 with tax_rate=21%.
        Net price = 10.95 / 1.21 = 9.05. This prevents double-counting tax.
        """
        if not data.line_items or data.net_amount is None or data.total_amount is None:
            return data

        # Sum of line item net_totals (the values OCR extracted)
        items_sum = sum(
            item.net_total for item in data.line_items
            if item.net_total is not None
        )

        if not items_sum:
            return data

        tolerance = Decimal("0.05")

        matches_total = abs(items_sum - data.total_amount) <= tolerance
        matches_net = abs(items_sum - data.net_amount) <= tolerance

        if matches_total and not matches_net:
            # Prices are tax-inclusive — strip embedded tax from each line item
            for item in data.line_items:
                if item.tax_rate and item.tax_rate > 0:
                    divisor = Decimal("1") + item.tax_rate / Decimal("100")
                    if item.unit_price is not None:
                        item.unit_price = (item.unit_price / divisor).quantize(Decimal("0.01"))
                    if item.gross_total is not None:
                        item.gross_total = (item.gross_total / divisor).quantize(Decimal("0.01"))
                    if item.net_total is not None:
                        item.net_total = (item.net_total / divisor).quantize(Decimal("0.01"))

        return data

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
