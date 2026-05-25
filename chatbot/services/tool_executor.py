"""
Safe tool dispatch with permission and company-scope enforcement.
"""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model

from chatbot.interfaces import BaseTool, ChatContext, ToolExecutionError
from chatbot.registry import ToolRegistry

User = get_user_model()
logger = logging.getLogger(__name__)


class ToolExecutor:
    """Validates permissions, then delegates to the tool's ``execute()``."""

    @staticmethod
    def execute(tool_name: str, context: ChatContext, params: dict) -> dict[str, Any]:
        """Run a tool by name within the given context.

        Returns a dict with ``{"success": True, "data": ...}``
        or ``{"success": False, "error": "..."}`` on failure.
        """
        tool = ToolRegistry.get_tool(tool_name)
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

        # ── Permission check ──────────────────────────────────────────────
        if tool.required_permission:
            try:
                user = User.objects.get(pk=context.user_id)
            except User.DoesNotExist:
                return {"success": False, "error": "User not found."}

            if not user.has_perm(tool.required_permission):
                return {
                    "success": False,
                    "error": f"Permission denied: {tool.required_permission}",
                }

        # ── Execute ───────────────────────────────────────────────────────
        try:
            data = tool.execute(context, params)
            return {"success": True, "data": data}
        except ToolExecutionError as exc:
            logger.warning("Tool %s failed: %s", tool_name, exc)
            return {"success": False, "error": str(exc)}
        except Exception:
            logger.exception("Unexpected error in tool %s", tool_name)
            return {"success": False, "error": f"Internal error executing {tool_name}."}
