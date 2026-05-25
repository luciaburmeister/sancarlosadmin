"""
Tool registry with auto-discovery.

On ``AppConfig.ready()`` the registry imports every module under
``chatbot/tools/`` so that ``@register_tool`` decorators fire and populate
the global registry.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chatbot.interfaces import BaseTool

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Singleton-style registry that maps tool names → tool instances."""

    _tools: dict[str, BaseTool] = {}

    # ── Public API ────────────────────────────────────────────────────────

    @classmethod
    def register(cls, tool: BaseTool) -> None:
        if tool.name in cls._tools:
            logger.warning("Tool %r registered twice – overwriting.", tool.name)
        cls._tools[tool.name] = tool
        logger.debug("Registered chatbot tool: %s", tool.name)

    @classmethod
    def get_tool(cls, name: str) -> BaseTool | None:
        return cls._tools.get(name)

    @classmethod
    def all_tools(cls) -> list[BaseTool]:
        return list(cls._tools.values())

    @classmethod
    def get_claude_tool_definitions(cls) -> list[dict]:
        """Return a list of tool definitions in Claude function-calling format."""
        definitions = []
        for tool in cls._tools.values():
            definitions.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.get_parameters_schema(),
            })
        return definitions

    # ── Auto-discovery ────────────────────────────────────────────────────

    @classmethod
    def autodiscover(cls) -> None:
        """Import all modules under ``chatbot.tools`` so decorators fire."""
        try:
            import chatbot.tools as tools_pkg
        except ImportError:
            logger.warning("chatbot.tools package not found – skipping autodiscovery.")
            return

        for _importer, modname, _ispkg in pkgutil.iter_modules(tools_pkg.__path__):
            full = f"chatbot.tools.{modname}"
            try:
                importlib.import_module(full)
                logger.debug("Auto-discovered tool module: %s", full)
            except Exception:
                logger.exception("Failed to import tool module %s", full)


def register_tool(cls):
    """Class decorator that instantiates and registers a BaseTool subclass.

    Usage::

        @register_tool
        class ListClients(BaseTool):
            ...
    """
    ToolRegistry.register(cls())
    return cls
