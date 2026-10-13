"""Claude generation of a ReportSpec limited to one dataset catalog.

Default used by ``ReportCreationService``. Replaces OpenAI's gpt-4.1-mini:
the hand-tuned system prompt this depends on (``spec_generation.SPEC_SYSTEM_PROMPT``)
is full of defensive instructions working around a model that doesn't
reliably follow them — a more capable model needs less of that, not more.
Shared validation lives in ``spec_generation.BaseReportSpecGenerator``; this
module only fetches the raw payload from Claude.
"""

from __future__ import annotations

from apps.reports.services.claude_client import (
    ClaudeAPIKeyMissing,
    ClaudeStructuredOutputError,
    ClaudeTool,
    ClaudeToolClient,
)
from apps.reports.services.spec_generation import (
    SPEC_JSON_SCHEMA,
    BaseReportSpecGenerator,
)


class ClaudeSpecError(RuntimeError):
    """Claude failed to produce a usable report spec."""


_EMIT_SPEC_TOOL = ClaudeTool(
    name="emit_report_spec",
    description="Emit the validated ERP report specification for this request.",
    schema=SPEC_JSON_SCHEMA,
)


class ClaudeReportSpecGenerator(BaseReportSpecGenerator):
    error_cls = ClaudeSpecError

    def _fetch_payload(self, system: str, user: str, *, api_key: str = "") -> dict:
        try:
            return ClaudeToolClient(_EMIT_SPEC_TOOL).invoke(
                system=system, user=user, api_key=api_key
            )
        except ClaudeAPIKeyMissing:
            raise
        except ClaudeStructuredOutputError as exc:
            raise ClaudeSpecError(str(exc)) from exc
