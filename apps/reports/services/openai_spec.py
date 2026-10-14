"""OpenAI generation of a ReportSpec limited to one dataset catalog.

Kept for rollback only — ``ClaudeReportSpecGenerator`` (see claude_spec.py)
is the default used by ``ReportCreationService``. Shared validation lives in
``spec_generation.BaseReportSpecGenerator``; this module only fetches the
raw payload from OpenAI.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from openai import OpenAI

from apps.reports.services.openai_client import create_structured_completion
from apps.reports.services.spec_generation import (
    SPEC_JSON_SCHEMA,
    SPEC_SYSTEM_PROMPT,
    BaseReportSpecGenerator,
    ReportGenerationContext,
    SpecGeneration,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SPEC_JSON_SCHEMA",
    "SPEC_SYSTEM_PROMPT",
    "ReportGenerationContext",
    "SpecGeneration",
    "OpenAISpecError",
    "OpenAIReportSpecGenerator",
]


class OpenAISpecError(RuntimeError):
    """OpenAI failed to produce a usable report spec."""


class OpenAIReportSpecGenerator(BaseReportSpecGenerator):
    error_cls = OpenAISpecError

    def _fetch_payload(self, system: str, user: str, *, api_key: str = "") -> dict:
        api_key = getattr(settings, "OPENAI_API_KEY", None)
        if not api_key:
            raise OpenAISpecError("OPENAI_API_KEY is not configured.")

        model = getattr(settings, "OPENAI_REPORT_MODEL", "gpt-4.1-mini")
        client = OpenAI(api_key=api_key)

        try:
            response = create_structured_completion(
                client,
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "report_spec",
                        "strict": True,
                        "schema": SPEC_JSON_SCHEMA,
                    },
                },
            )
        except Exception as exc:
            logger.exception("OpenAI report spec generation failed")
            raise OpenAISpecError(str(exc)) from exc

        content = response.choices[0].message.content
        if not content:
            raise OpenAISpecError("OpenAI returned an empty response.")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAISpecError("OpenAI returned invalid JSON.") from exc
