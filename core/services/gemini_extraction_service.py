"""
Base for Gemini-powered document extraction (expense tickets, invoices, etc.).

Subclasses provide MODEL_NAME, SCHEMA and PROMPT, and implement _to_dataclass
to convert the raw JSON response into their own dataclass.
"""

from abc import ABC, abstractmethod
from typing import Dict

import google.genai as genai
from google.genai import types
import json


class GeminiJSONParseError(Exception):
    """Gemini returned text that isn't valid JSON. Carries the raw text so
    callers can persist it (e.g. to Invoice.ocr_raw_response) for debugging."""

    def __init__(self, message: str, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


class BaseGeminiExtractor(ABC):
    MODEL_NAME = "gemini-3-flash-preview"
    FALLBACK_MODEL_NAME = "gemini-2.5-flash"
    SCHEMA: dict = {}
    PROMPT: str = ""

    def __init__(self, api_key: str):
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=5)
            ),
        )

    def extract_data(self, file_path: str):
        uploaded_file = self.client.files.upload(file=file_path)

        models_to_try = [self.MODEL_NAME, self.FALLBACK_MODEL_NAME]
        data = None
        for i, model_name in enumerate(models_to_try):
            try:
                data = self._call_model(model_name, uploaded_file)
                break
            except Exception:
                is_last = i == len(models_to_try) - 1
                if is_last:
                    raise

        return self._to_dataclass(data)

    def _call_model(self, model_name: str, uploaded_file) -> Dict:
        """
        Call the given Gemini model with the uploaded file + prompt and parse its JSON response.
        Raises GeminiJSONParseError (carrying the raw text) if Gemini's response isn't valid JSON.
        """
        response = self.client.models.generate_content(
            model=model_name,
            contents=[uploaded_file, self.PROMPT],
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
                raise GeminiJSONParseError(f"Gemini returned invalid JSON: {e}", raw_text=raw) from e
        elif isinstance(raw, dict):
            return raw
        else:
            raise TypeError(f"Unexpected Gemini JSON type: {type(raw)}")

    @abstractmethod
    def _to_dataclass(self, data: Dict):
        """Convert the raw Gemini JSON dict into the provider-specific dataclass."""
        raise NotImplementedError
