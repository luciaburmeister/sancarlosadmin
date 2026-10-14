"""Claude classifier for report prompts.

Used by ``build_classifier()``: picking one domain out of a fixed list
with a confidence score is a structured-output task a frontier model
handles natively, without an extra provider/API key.
"""

from __future__ import annotations

from apps.reports.services.classification import (
    ReportClassification,
    ReportClassificationPrompt,
)
from apps.reports.services.claude_client import (
    ClaudeAPIKeyMissing,
    ClaudeStructuredOutputError,
    ClaudeTool,
    ClaudeToolClient,
)


class ClaudeClassifierError(RuntimeError):
    """Claude failed to classify the report prompt."""


_CLASSIFY_TOOL = ClaudeTool(
    name=ReportClassificationPrompt.TOOL_NAME,
    description=ReportClassificationPrompt.TOOL_DESCRIPTION,
    schema=ReportClassificationPrompt.SCHEMA,
    max_tokens=256,
)


class ClaudeReportClassifier:
    def classify(self, prompt: str, *, api_key: str) -> ReportClassification:
        try:
            payload = ClaudeToolClient(_CLASSIFY_TOOL).invoke(
                system=ReportClassificationPrompt.system_prompt(),
                user=prompt,
                api_key=api_key,
            )
        except ClaudeAPIKeyMissing:
            raise
        except ClaudeStructuredOutputError as exc:
            raise ClaudeClassifierError(str(exc)) from exc

        domain = payload.get("domain")
        if not domain:
            raise ClaudeClassifierError("Claude did not return a domain.")

        return ReportClassification(
            domain=str(domain),
            confidence=float(payload.get("confidence") or 0),
            needs_clarification=bool(payload.get("needs_clarification")),
            source="claude",
        )
