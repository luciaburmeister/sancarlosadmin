"""
Gemini-powered matcher for purchase invoice lines against purchase order lines.

Unlike GeminiExpenseProcessor (BaseGeminiExtractor), this is a text-only call:
there is no document to upload, just a structured comparison prompt built from
invoice lines and PO lines already in the database. Mirrors BaseGeminiExtractor's
model+fallback and JSON-schema pattern without inheriting from it.
"""
import json
import logging
from typing import Dict, List

import google.genai as genai
from google.genai import types

logger = logging.getLogger(__name__)


class GeminiPOMatchParseError(Exception):
    """Gemini returned text that isn't valid JSON. Carries the raw text so
    callers can log it for debugging."""

    def __init__(self, message: str, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


class GeminiPOItemMatcher:
    MODEL_NAME = "gemini-3-flash-preview"
    FALLBACK_MODEL_NAME = "gemini-2.5-flash"

    SCHEMA = {
        "type": "OBJECT",
        "required": ["matches"],
        "properties": {
            "matches": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": ["invoice_item_id", "po_item_id", "quantity", "confidence", "reasoning"],
                    "properties": {
                        "invoice_item_id": {"type": "INTEGER"},
                        "po_item_id": {"type": "INTEGER"},
                        "quantity": {
                            "type": "NUMBER",
                            "description": (
                                "Quantity from this invoice line allocated to this "
                                "specific PO line. Must not exceed that PO line's "
                                "pending_quantity."
                            ),
                        },
                        "confidence": {
                            "type": "STRING",
                            "enum": ["high", "medium", "low"],
                        },
                        "reasoning": {
                            "type": "STRING",
                            "description": (
                                "Short justification citing the signals used "
                                "(price, quantity, prior matches on this invoice, "
                                "description)."
                            ),
                        },
                    },
                },
            },
        },
    }

    PROMPT_HEADER = """
    You are matching lines from a purchase invoice against lines from candidate
    purchase orders (POs) belonging to the same provider.

    A provider is often billed repeatedly for the same items across many POs, so
    the item description alone is NOT reliable — the same item name can appear
    on several different POs. Use this priority order when deciding a match:

    1. Unit price: two lines with the same or very close unit price are far more
       likely to be the same order than lines that merely share a name.
    2. Available quantity: a PO line can only be suggested if its
       pending_quantity is enough to cover, fully or partially, the invoice
       line's remaining quantity. Never suggest a quantity greater than the PO
       line's pending_quantity.
    3. Purchase orders already used elsewhere in this same invoice
       (already_matched_in_this_invoice: true): if other lines on this invoice
       already matched a specific PO, strongly prefer other pending lines from
       that same PO for the remaining invoice lines, unless price or quantity
       clearly rule it out.
    4. Description similarity: use this only as supporting evidence, since
       invoice OCR text and PO item names frequently differ in wording.

    Evaluate each invoice line independently and only report a match when you
    are genuinely confident — being the least-bad candidate among weak options
    is NOT the same as a genuine match. If no PO line is a safe match for an
    invoice line, omit that invoice line from the result entirely rather than
    forcing a low-confidence guess. Only "high" confidence matches will be
    shown to the user for confirmation; "medium"/"low" exist for calibration
    only and will not be persisted.

    Return quantity as a plain number using the invoice line's own unit.
    """

    def __init__(self, api_key: str):
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=5)
            ),
        )

    def find_matches(
        self,
        invoice_items_data: List[Dict],
        po_candidates_data: List[Dict],
    ) -> List[Dict]:
        """
        invoice_items_data: [{"id", "description", "quantity", "unit_price"}, ...]
        po_candidates_data: [
            {
                "po_id", "po_number", "date_ordered", "already_matched_in_this_invoice",
                "items": [{"id", "name", "description", "unit_price", "pending_quantity"}, ...]
            }, ...
        ]

        Returns the raw "matches" list from Gemini, all confidence levels
        included — the caller filters to "high" before persisting anything.
        """
        prompt = self._build_prompt(invoice_items_data, po_candidates_data)

        models_to_try = [self.MODEL_NAME, self.FALLBACK_MODEL_NAME]
        data = None
        for i, model_name in enumerate(models_to_try):
            try:
                data = self._call_model(model_name, prompt)
                break
            except Exception:
                is_last = i == len(models_to_try) - 1
                if is_last:
                    raise
                logger.warning(
                    "GeminiPOItemMatcher: model %s failed, falling back to %s",
                    model_name, models_to_try[i + 1],
                )

        return data.get("matches", [])

    def _build_prompt(self, invoice_items_data, po_candidates_data) -> str:
        payload = {
            "invoice_lines_to_match": invoice_items_data,
            "candidate_purchase_orders": po_candidates_data,
        }
        return self.PROMPT_HEADER + "\n\nData:\n" + json.dumps(payload, ensure_ascii=False)

    def _call_model(self, model_name: str, prompt: str) -> Dict:
        response = self.client.models.generate_content(
            model=model_name,
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=self.SCHEMA,
                temperature=0.0,
            ),
        )

        part = response.candidates[0].content.parts[0]
        raw = json.loads(part.json())["text"]

        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise GeminiPOMatchParseError(f"Gemini returned invalid JSON: {e}", raw_text=raw) from e
        elif isinstance(raw, dict):
            return raw
        else:
            raise TypeError(f"Unexpected Gemini JSON type: {type(raw)}")