"""Shared OpenAI chat-completions call for the report services.

Centralizes the reasoning_effort handling: OPENAI_REPORT_REASONING_EFFORT
is only supported by reasoning-capable models (e.g. gpt-6-luna), so if the
deployment is configured with an older model that rejects the argument,
this retries once without it instead of hard-failing every request.
"""

from __future__ import annotations

from django.conf import settings
from openai import BadRequestError, OpenAI


def create_structured_completion(client: OpenAI, **kwargs):
    reasoning_effort = getattr(settings, "OPENAI_REPORT_REASONING_EFFORT", None)
    if not reasoning_effort:
        return client.chat.completions.create(**kwargs)
    try:
        return client.chat.completions.create(
            reasoning_effort=reasoning_effort, **kwargs
        )
    except BadRequestError as exc:
        if "reasoning_effort" not in str(exc):
            raise
        return client.chat.completions.create(**kwargs)
