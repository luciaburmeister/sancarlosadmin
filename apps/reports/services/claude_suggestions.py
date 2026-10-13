"""Claude generation of more specific report-prompt rewrites.

Default used by ``ReportCreationService`` when a request is too vague to
classify. This module owns the tool and the system prompt; the Anthropic
call lives in ``ClaudeToolClient``.
"""

from __future__ import annotations

import logging

from apps.reports.datasets import DatasetAccess
from apps.reports.services.claude_client import (
    ClaudeStructuredOutputError,
    ClaudeTool,
    ClaudeToolClient,
)

logger = logging.getLogger(__name__)

_SUGGEST_TOOL = ClaudeTool(
    name="suggest_report_prompts",
    description=(
        "Suggest 2-3 more specific rewrites of a too-vague ERP report request."
    ),
    schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["suggestions"],
        "properties": {
            "suggestions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string"},
            },
        },
    },
    max_tokens=512,
)


class ClaudeReportPromptSuggester:
    def suggest(
        self,
        original_prompt: str,
        available_datasets: list[DatasetAccess],
        *,
        api_key: str,
    ) -> list[str]:
        """Ask Claude for 2-3 more specific rewrites of a too-vague prompt.

        Best-effort only: returns an empty list on any failure so a broken
        or unconfigured Anthropic key never blocks the underlying
        classification error the user already sees.
        """

        accessible = [d for d in available_datasets if d.available]
        if not accessible:
            return []

        domains = ", ".join(str(d.label) for d in accessible)
        system = (
            "The user asked for an ERP report but the request was too vague "
            "to build (no clear metric, grouping, filter, or time range). "
            "Suggest 2-3 short, concrete rewrites of their request, in the "
            "same language they used, that would be specific enough to "
            "build a report. Only suggest requests about these domains the "
            f"user has access to: {domains}. Each suggestion must name a "
            "metric/entity and at least one filter or time range. Keep "
            "each suggestion under 20 words."
        )

        try:
            payload = ClaudeToolClient(_SUGGEST_TOOL).invoke(
                system=system,
                user=original_prompt,
                api_key=api_key,
            )
        except ClaudeStructuredOutputError:
            logger.exception("Claude prompt suggestion generation failed")
            return []

        suggestions = payload.get("suggestions")
        if not isinstance(suggestions, list):
            return []
        return [str(s).strip() for s in suggestions if str(s).strip()][:3]
