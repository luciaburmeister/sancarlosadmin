"""
Gemini Account Classifier

Connects to Gemini and asks it to classify a batch of invoices
into the correct accounting account from the chart of accounts.
"""

import json
import google.genai as genai
from google.genai import types
from typing import List

from .account_classifier_interface import (
    AccountClassifierInterface,
    AccountOption,
    ClassificationResult,
    ClassificationBatchResponse,
)

class GeminiAccountClassifier(AccountClassifierInterface):

    # The schema we ask Gemini to return.
    # A list of objects — one per invoice — each with:
    #   - invoice_id: the PK we passed in
    #   - account_code: the code it chose, or null if unsure
    #   - confident: true/false
    CLASSIFICATION_SCHEMA = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "required": ["invoice_id", "line_item_id", "account_code", "confident"],
            "properties": {
                "invoice_id": {
                    "type": "INTEGER",
                    "description": "The invoice ID exactly as provided in the input.",
                },
                "line_item_id": {
                    "type": "INTEGER",
                    "description": "The line item ID exactly as provided in the input.",
                },
                "account_code": {
                    "type": "STRING",
                    "nullable": True,
                    "description": (
                        "The account code from the chart of accounts that best matches "
                        "this line item. Must be one of the provided codes. "
                        "Set to null if you are not confident."
                    ),
                },
                "confident": {
                    "type": "BOOLEAN",
                    "description": (
                        "True if you are confident in the classification. "
                        "False if the item is ambiguous or no account fits well."
                    ),
                },
            },
        },
    }

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.model_name = "gemini-3-flash-preview"

    def classify_invoices(
        self,
        invoices: List[dict],
        account_options: List[AccountOption],
        company_activity_description: str = "",
        accounting_framework: str = "",
    ) -> ClassificationBatchResponse:
        """
        Send all unclassified invoices + the full chart of accounts to Gemini
        in a single call and get back one classification per invoice.
        """

        # Build the accounts list as a readable string for the prompt
        accounts_text = "\n".join(
            f"  {acc.code} - {acc.name}" for acc in account_options
        )

        # Build the invoices list as a readable string for the prompt
        invoices_text = ""
        for inv in invoices:
            invoices_text += f"\nInvoice ID: {inv['id']}\nLine items:\n"
            for item in inv["line_items"]:
                invoices_text += (
                    f"  - [LineItem ID: {item.id}] {item.description or 'N/A'} "
                    f"(qty: {item.quantity}, unit price: {item.unit_price}, "
                    f"total: {item.total}, tax: {item.tax_rate}%)\n"
                )

        framework_line = f"using {accounting_framework}" if accounting_framework else ""
        prompt = f"""

You are an expert accountant {framework_line}.
Your task: classify each invoice into exactly one accounting account.

RULES:
- You MUST only pick account codes from the list provided below.
- One account per line item.
- If you are not confident, set account_code to null and confident to false.
- Base your decision on the line item descriptions, not the amounts.
- Return one result per line item, using the exact invoice_id and line_item_id provided.

CHART OF ACCOUNTS (postable accounts only):
{accounts_text}

INVOICES TO CLASSIFY:
{invoices_text}

COMPANY ACTIVITY DESCRIPTION:
{company_activity_description}


"""

        response = self.client.models.generate_content(
            model=self.model_name,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=self.CLASSIFICATION_SCHEMA,
                temperature=0.0,  # Deterministic accounting decisions
            ),
        )

        part = response.candidates[0].content.parts[0]
        raw = json.loads(part.json())["text"]

        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, list):
            data = raw
        else:
            raise ValueError(f"Unexpected Gemini response type: {type(raw)}")

        results = [
            ClassificationResult(
                invoice_id=item["invoice_id"],
                line_item_id=item["line_item_id"],
                account_code=item.get("account_code"),
                confident=item.get("confident", False),
            )
            for item in data
        ]

        return ClassificationBatchResponse(
            results=results,
            raw_response=data,
            metadata={"model": self.model_name}
        )