"""
Core abstractions for the ERP chatbot tool system.

Every tool that exposes ERP data to the LLM must implement the ``BaseTool``
interface. This guarantees:

1. **Read-only** – tools never mutate data.
2. **Company-scoped** – every query is filtered by ``context.company_id``.
3. **Permission-checked** – tools can declare a required Django permission.
4. **LLM-compatible** – ``get_parameters_schema()`` produces JSON Schema that
   Claude (or any function-calling LLM) can consume directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatContext:
    """Immutable context passed to every tool execution.

    Attributes:
        company_id: PK of the ``Company`` the user selected for this session.
        user_id: PK of the authenticated Django ``User``.
        company_name: Human-readable company name (for system prompts).
    """
    company_id: int
    user_id: int
    company_name: str = ""


class ToolExecutionError(Exception):
    """Raised when a tool execution fails in a controlled way.

    The ``message`` is safe to surface to the LLM as a tool-result error.
    """
    pass


class BaseTool(ABC):
    """Interface that all ERP chatbot tools must implement.

    Subclasses are automatically registered when placed inside
    ``chatbot/tools/`` and decorated with ``@register_tool``.
    """

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name in ``snake_case``.  E.g. ``'list_clients'``."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable one-liner consumed by the LLM system prompt."""

    # ── Schema ────────────────────────────────────────────────────────────

    @abstractmethod
    def get_parameters_schema(self) -> dict:
        """Return a JSON Schema ``object`` describing the tool's inputs.

        Example::

            {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Search term"},
                    "limit":  {"type": "integer", "description": "Max results", "default": 25},
                },
                "required": [],
            }
        """

    # ── Execution ─────────────────────────────────────────────────────────

    @abstractmethod
    def execute(self, context: ChatContext, params: dict) -> dict:
        """Execute the tool and return a JSON-serialisable dict.

        Contract:
        * **Must be read-only** – never write/update/delete rows.
        * **Must scope queries** to ``context.company_id``.
        * Raise ``ToolExecutionError`` for expected failures.
        """

    # ── Optional hooks ────────────────────────────────────────────────────

    @property
    def required_permission(self) -> str | None:
        """Optional Django permission string.  E.g. ``'sales.view_client'``.

        When set, the tool executor will check
        ``user.has_perm(required_permission)`` before calling ``execute()``.
        """
        return None
