"""
Maps raw responses to extracted_dataclass structure
"""

import google.generativeai as genai
import json
from typing import Dict, Optional
from decimal import Decimal
from datetime import datetime
from .ocr_interface import OcrInterface, InvoiceData, LineItem, TaxItem

class GeminiInvoiceProcessor(OcrInterface):
    
    def __init__(self, api_key: str):
        """
        Initialize the processor.
        Args: API key for Gemini API (in .evn file)
        """
        # Configure API Key 
        genai.configure(api_key=api_key) #Sets the API key globally for the entire package
        self.model = genai.GenerativeModel("gemini-flash-latest")
    
    def extract_data(self, file_path: str) -> InvoiceData:
        """
        Extract invoice data from a file.
        Args:
            file_path: Full path to the invoice file (PDF, PNG, JPG)
        Returns:
            Dictionary with extracted invoice data
        """
        
        # Determine mime type
        mime_type = self._get_mime_type(file_path)
        
        # Upload file to Gemini (this reads the file internally)
        sample_file = genai.upload_file(path=file_path, mime_type=mime_type)
        print(f"File uploaded: {sample_file.uri}") #the url where the file is stored in Gemini
        
        # Create the prompt
        prompt = """
        Extract ALL information from this invoice and return it as JSON.
        
        Return ONLY valid JSON with this EXACT structure (no markdown, no ```json):
        {
            "vendor_name": "Full company name",
            "vendor_tax_id": "NIF/CIF/VAT number",
            "invoice_number": "Invoice number/ID",
            "date_issued": "YYYY-MM-DD",
            "due_date": "YYYY-MM-DD",
            "total_amount": 1234.56,
            "currency": "EUR",
            "payment_method": "transfer or cash or card",
            "line_items": [
                {
                    "description": "Product/service description",
                    "quantity": 1.0,
                    "unit_price": 100.0,
                    "total": 100.0,
                    "tax_rate": 21.0
                }
            ],
            "taxes": [
                {
                    "name": "IVA/VAT/Tax name",
                    "percentage": 21.0,
                    "base_amount": 100.00,
                    "tax_amount": 21.00
                }
            ]
        }
        
        CRITICAL RULES:
        1. vendor_tax_id: Extract NIF, CIF, VAT number (usually near company name)
        2. line_items: Each item MUST include tax_rate (which IVA/VAT applies)
        3. taxes: MUST include base_amount (what was taxed) and tax_amount (the tax itself)
        4. Use null for missing values
        5. Dates must be YYYY-MM-DD format
        6. Numbers must be numeric (not strings)
        7. Return ONLY the JSON object, NO other text
        8. For payment_method, use: "transfer", "cash", or "card"
        
        IMPORTANT: If there are multiple tax rates (e.g., IVA 4%, 10%, 21%), 
        include separate entries in the taxes array for each rate.
        """
    
        #Call Gemini API (Send the invoice + instructions)
        response = self.model.generate_content([sample_file, prompt])
        # Extract text from response
        raw_text = response.text
        
        # Clean and parse response
        raw_json = raw_text.replace('```json', '').replace('```', '').strip()
        print(f"Raw response: {raw_json[:200]}...")

        # Parse JSON
        try:
            data = json.loads(raw_json) #Convert JSON string to Python dictionary
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON from Gemini: {e}\n{raw_json[:500]}")
        

        # Normalize data
        return self._convert_to_dataclass(data)
    

    def _convert_to_dataclass(self, data: Dict) -> InvoiceData:
        """
        Convert raw dictionary to InvoiceData dataclass.
        """
        
        # Convert line items
        line_items = []
        for item_data in data.get('line_items', []):
            line_items.append(LineItem(
                description=item_data.get('description'),
                quantity=self._to_decimal(item_data.get('quantity')),
                unit_price=self._to_decimal(item_data.get('unit_price')),
                total=self._to_decimal(item_data.get('total')),
                tax_rate=self._to_decimal(item_data.get('tax_rate'))
            ))
        
        # Convert taxes
        taxes = []
        for tax_data in data.get('taxes', []):
            taxes.append(TaxItem(
                name=tax_data.get('name'),
                percentage=self._to_decimal(tax_data.get('percentage')),
                base_amount=self._to_decimal(tax_data.get('base_amount')),
                tax_amount=self._to_decimal(tax_data.get('tax_amount'))
            ))
        
        # Create and return InvoiceData
        return InvoiceData(
            vendor_name=data.get('vendor_name'),
            vendor_tax_id=data.get('vendor_tax_id'),
            invoice_number=data.get('invoice_number'),
            date_issued=self._parse_date(data.get('date_issued')),
            due_date=self._parse_date(data.get('due_date')),
            total_amount=self._to_decimal(data.get('total_amount')),
            currency=data.get('currency', 'EUR'),
            payment_method=self._normalize_payment_method(data.get('payment_method')),
            line_items=line_items,
            taxes=taxes
        )


    def _get_mime_type(self, file_path: str) -> str:
        """
        Determine MIME type from file extension.
        """
        extension = file_path.lower().split('.')[-1] #split by . and get last part
        
        mime_types = {
            'pdf': 'application/pdf',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
        }
        
        return mime_types.get(extension, 'application/pdf') #default is set to pdf



    def _normalize_payment_method(self, method: Optional[str]) -> Optional[str]:
        """
        Normalize payment method to match Django choices
        Returns: 'transfer', 'cash', 'card', or None
        """
        if not method:
            return None
        
        method_lower = method.lower()
        
        if 'transfer' in method_lower or 'bank' in method_lower or 'bancaria' in method_lower:
            return 'transfer'
        elif 'cash' in method_lower or 'efectivo' in method_lower or 'contado' in method_lower:
            return 'cash'
        elif 'card' in method_lower or 'tarjeta' in method_lower or 'credit' in method_lower:
            return 'card'
        
        return None
    

    def _parse_date(self, date_str: Optional[str]) -> Optional[str]:
        """
        Validate and return date string in YYYY-MM-DD format.
        Returns: String like "2024-01-15" or None
        """
        if not date_str:
            return None
        
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
            return date_str
        except ValueError:
            return None
        
    
    def _to_decimal(self, value) -> Optional[Decimal]:
        """
        Convert value to Decimal safely.
        Returns: Decimal or None
        """
        if value is None:
            return None
        
        try:
            return Decimal(str(value)) # Decimal constructor works best with strings
        except:
            return None








    
    
