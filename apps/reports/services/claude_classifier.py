"""Claude classifier for report prompts.

Used by ``build_classifier()``: picking one or more domains out of a fixed
list with a confidence score is a structured-output task a frontier model
handles natively, without an extra provider/API key.
"""

from __future__ import annotations

from apps.reports.constants import MAX_REPORT_SECTIONS
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

        domains = _domains_from_payload(payload)
        if not domains:
            raise ClaudeClassifierError("Claude did not return a domain.")

        return ReportClassification(
            domain=domains[0],
            domains=domains,
            confidence=float(payload.get("confidence") or 0),
            needs_clarification=bool(payload.get("needs_clarification")),
            source="claude",
        )


def _domains_from_payload(payload: dict) -> list[str]:
    raw = payload.get("domains")
    if not isinstance(raw, list) or not raw:
        single = payload.get("domain")
        raw = [single] if single else []
    domains: list[str] = []
    for item in raw:
        value = str(item)
        if value not in domains:
            domains.append(value)
        if len(domains) >= MAX_REPORT_SECTIONS:
            break
    return domains
