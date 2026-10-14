"""Core abstractions for the ERP AI agent's tool system.

Every tool that exposes ERP data to the LLM must implement ``BaseTool``:

1. **Read-only** – tools never mutate data.
2. **Company-scoped** – every query is filtered to the user's companies.
3. **Permission-checked** – tools declare a required Django permission.
4. **LLM-compatible** – ``get_parameters_schema()`` produces JSON Schema
   Claude can consume directly as a tool definition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatContext:
    """Immutable context passed to every tool execution.

    Attributes:
        user_id: PK of the authenticated Django ``User``.
        conversation_id: PK of the current ``AIConversation``, for the few
            tools (artifact generation) that need to attach something to it.
    """

    user_id: int
    conversation_id: int | None = None


class ToolExecutionError(Exception):
    """Raised when a tool execution fails in a controlled way.

    The ``message`` is safe to surface to the LLM as a tool-result error.
    """


class BaseTool(ABC):
    """Interface every AI agent tool must implement.

    Subclasses are automatically registered when placed inside
    ``apps/ai_agent/tools/`` and decorated with ``@register_tool``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name in ``snake_case``. E.g. ``'get_entity'``."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable one-liner consumed by the LLM as the tool description."""

    @abstractmethod
    def get_parameters_schema(self) -> dict:
        """Return a JSON Schema ``object`` describing the tool's inputs."""

    @abstractmethod
    def execute(self, context: ChatContext, params: dict) -> dict:
        """Execute the tool and return a JSON-serialisable dict.

        Contract:
        * **Must be read-only** – never write/update/delete rows.
        * **Must scope queries** to the user's companies.
        * Raise ``ToolExecutionError`` for expected failures.
        """

    @property
    def required_permission(self) -> str | None:
        """Optional Django permission string, e.g. ``'sales.view_client'``.

        When set, the tool executor checks ``user.has_perm(...)`` before
        calling ``execute()``.
        """
        return None
