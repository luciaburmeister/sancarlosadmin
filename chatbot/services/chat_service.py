"""
Chat service — orchestrates Claude function-calling loop.

Flow:
1. Receive user message + conversation history
2. Build system prompt with company context
3. Send to Claude with tool manifest
4. If Claude returns tool_use blocks → dispatch via ToolExecutor
5. Feed tool results back to Claude
6. Return final markdown response
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from django.conf import settings

from chatbot.interfaces import ChatContext
from chatbot.registry import ToolRegistry
from chatbot.services.tool_executor import ToolExecutor

logger = logging.getLogger(__name__)

# Maximum tool-call rounds to prevent infinite loops
MAX_TOOL_ROUNDS = 10


def _build_system_prompt(context: ChatContext) -> str:
    """Build the system prompt injected at the start of every conversation."""
    return (
        f"You are an intelligent assistant for the ERP system of **{context.company_name}**.\n\n"
        "## Rules\n"
        "- You have access to a set of **read-only** tools that query the ERP database.\n"
        "- All data is scoped to the current company — you cannot access other companies.\n"
        "- Format your responses in **Markdown** with tables, headers, and bullet points.\n"
        "- When showing monetary amounts, always include the currency (EUR).\n"
        "- When showing dates, use DD/MM/YYYY format.\n"
        "- If the user asks to create, modify, or delete anything, politely explain "
        "that you are read-only and can only answer questions.\n"
        "- Be concise but thorough.\n"
        "- Respond in the same language the user uses.\n"
    )


class ChatService:
    """Stateless service that drives a single chat turn."""

    def __init__(self):
        api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not configured in settings. "
                "Add it to your .env file."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    def chat(
        self,
        context: ChatContext,
        message: str,
        history: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Process a user message and return the assistant's response.

        Args:
            context: Company + user scope for this session.
            message: The user's latest message text.
            history: Previous conversation messages (Claude format).

        Returns:
            {
                "response": "markdown string",
                "tools_used": ["tool_name", ...],
                "history": [updated conversation messages],
            }
        """
        system_prompt = _build_system_prompt(context)
        tools = ToolRegistry.get_claude_tool_definitions()

        # Build messages list: history + new user message
        messages = list(history or [])
        messages.append({"role": "user", "content": message})

        tools_used: list[str] = []

        for _round in range(MAX_TOOL_ROUNDS):
            # Call Claude
            response = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system_prompt,
                tools=tools if tools else anthropic.NOT_GIVEN,
                messages=messages,
            )

            # Check if Claude wants to call tools
            tool_use_blocks = [
                block for block in response.content
                if block.type == "tool_use"
            ]

            if not tool_use_blocks:
                # No tool calls — extract the final text response
                text_parts = [
                    block.text
                    for block in response.content
                    if block.type == "text"
                ]
                final_text = "\n".join(text_parts)

                # Append assistant message to history
                messages.append({"role": "assistant", "content": response.content})

                return {
                    "response": final_text,
                    "tools_used": tools_used,
                    "history": messages,
                }

            # Claude wants to use tools — execute them
            # First, add the assistant's response (with tool_use blocks) to messages
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool and build tool_result blocks
            tool_results = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input or {}
                tools_used.append(tool_name)

                logger.info(
                    "Executing tool %s with params: %s",
                    tool_name,
                    json.dumps(tool_input, default=str)[:500],
                )

                result = ToolExecutor.execute(tool_name, context, tool_input)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": json.dumps(result, default=str, ensure_ascii=False),
                })

            # Add tool results as a user message
            messages.append({"role": "user", "content": tool_results})

        # Safety: max rounds reached
        return {
            "response": "I've reached the maximum number of tool calls. Please try a simpler question.",
            "tools_used": tools_used,
            "history": messages,
        }
