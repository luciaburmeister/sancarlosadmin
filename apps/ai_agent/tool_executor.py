"""Safe tool dispatch with permission enforcement."""

from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model

from apps.ai_agent.interfaces import BaseTool, ChatContext, ToolExecutionError
from apps.ai_agent.registry import ToolRegistry

User = get_user_model()
logger = logging.getLogger(__name__)


class ToolExecutor:
    """Validates permissions, then delegates to the tool's ``execute()``."""

    @staticmethod
    def execute(tool_name: str, context: ChatContext, params: dict) -> dict[str, Any]:
        """Run a tool by name within the given context.

        Returns ``{"success": True, "data": ...}`` or
        ``{"success": False, "error": "..."}`` on failure. Never raises —
        the caller feeds this straight back to the LLM as a tool result.
        """
        tool = ToolRegistry.get_tool(tool_name)
        if tool is None:
            return {"success": False, "error": f"Unknown tool: {tool_name}"}

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

        try:
            data = tool.execute(context, params)
            return {"success": True, "data": data}
        except ToolExecutionError as exc:
            logger.warning("Tool %s failed: %s", tool_name, exc)
            return {"success": False, "error": str(exc)}
        except Exception:
            logger.exception("Unexpected error in tool %s", tool_name)
            return {"success": False, "error": f"Internal error executing {tool_name}."}
