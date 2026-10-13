"""Shared Anthropic forced-tool-use call for the report services.

Mirrors ``openai_client.create_structured_completion``: one tool call
matching ``ClaudeTool.schema`` so the response is always a JSON object
shaped like the schema.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
from django.conf import settings


class ClaudeStructuredOutputError(RuntimeError):
    """Claude did not return the forced tool call, or the API call failed."""


class ClaudeAPIKeyMissing(ClaudeStructuredOutputError):
    """The company has no Claude API key configured."""


@dataclass(frozen=True)
class ClaudeTool:
    name: str
    description: str
    schema: dict
    max_tokens: int = 2048


class ClaudeToolClient:
    """One forced tool call. The tool is fixed; system and user vary per request."""

    def __init__(self, tool: ClaudeTool):
        self.tool = tool

    def invoke(self, *, system: str, user: str, api_key: str) -> dict:
        if not api_key:
            raise ClaudeAPIKeyMissing("Claude API key is not configured.")

        model = getattr(settings, "ANTHROPIC_REPORT_MODEL", "claude-sonnet-5-5")
        client = anthropic.Anthropic(api_key=api_key)
        system_with_tool_instruction = (
            f"{system}\n\n"
            f"You MUST answer by calling the `{self.tool.name}` tool exactly once. "
            "Never respond in plain text."
        )
        try:
            response = client.messages.create(
                model=model,
                max_tokens=self.tool.max_tokens,
                system=system_with_tool_instruction,
                messages=[{"role": "user", "content": user}],
                tools=[
                    {
                        "name": self.tool.name,
                        "description": self.tool.description,
                        "input_schema": self.tool.schema,
                    }
                ],
                tool_choice={"type": "auto"},
            )
        except Exception as exc:
            raise ClaudeStructuredOutputError(str(exc)) from exc

        for block in response.content:
            if block.type == "tool_use" and block.name == self.tool.name:
                return block.input

        raise ClaudeStructuredOutputError(
            "Claude did not return the expected tool call."
        )
