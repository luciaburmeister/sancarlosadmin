"""Orchestrator — the agent's Claude tool-calling loop, streamed live.

Every entry appended to the in-memory ``messages`` list for this turn is
mirrored 1:1 as a persisted ``AIMessage`` row, so next turn's history
reconstruction is exact — never a hand-summarized approximation.

Streaming design: a tool_use block's name is known the instant it starts
(before its input even finishes streaming), so ``stream_turn()`` yields a
"step" event right then for instant feedback, executes the tool the
instant its block *stops* (full input now parsed) — not after the whole
round finishes — and streams the final answer's text token by token.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

import anthropic
from django.conf import settings
from django.utils.translation import gettext as _

from apps.ai_agent.interfaces import ChatContext
from apps.ai_agent.models import AIConversation, AIMessage
from apps.ai_agent.registry import ToolRegistry
from apps.ai_agent.state import ConversationState
from apps.ai_agent.tool_executor import ToolExecutor
from apps.reports.constants import ReportDomain
from apps.reports.datasets import get_dataset
from apps.reports.services.compiler import is_time_bucket

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10
# Older turns stay stored for the UI. Only the newest slice is sent back to
# the model, always starting on a real user question so a tool call and its
# result are never split.
MAX_HISTORY_MESSAGES = 16
AGENT_EFFORT = "medium"
PROMPT_CACHE_CONTROL = {"type": "ephemeral"}
THINKING_BETWEEN_TOOLS = {"type": "between_tools"}

SYSTEM_PROMPT_RULES = (
    "You are the ERP assistant for this company group. You help with "
    "questions, analysis, and reports across every module — clients, "
    "expedientes, quotes, invoices, payments, production, inventory, "
    "purchasing, accounting, payroll, and more.\n\n"
    "## Rules\n"
    "- You are READ-ONLY. If asked to create, modify, or delete anything, "
    "say so plainly and explain you can only query and report.\n"
    "- You have generic tools, not one per question: list_datasets, "
    "describe_dataset, search_entities, get_entity, traverse_relation, "
    "aggregate, generate_artifact. Call list_datasets/describe_dataset to "
    "learn a dataset's fields before filtering/aggregating/traversing it "
    "for the first time this conversation.\n"
    "- Every tool is already scoped to the current user's companies and "
    "permissions — never ask which company, and if a tool denies "
    "permission, tell the user they lack that permission, don't retry.\n"
    "- Use the conversation state below to resolve references like 'that "
    "client', 'those invoices', 'the pending ones' — then re-fetch the "
    "data with a tool call. Never answer from memory of an earlier result; "
    "the ERP is always the source of truth.\n"
    "- To build a document (PDF/Excel/PowerPoint), call generate_artifact "
    "with one or more `sections` (up to 5) — one per dataset the user "
    "wants combined into this single document (e.g. sales + production "
    "in one PDF), each with the same dataset/dimensions/aggregations/"
    "filters you would use for aggregate(), and the same chart/"
    "chart_label_dimension/chart_value_aggregation when a chart helps, "
    "plus up to 5 per-section `highlights`. Also pass a document-level "
    "`summary` (1-3 sentences) whenever the document should read as a "
    "report of findings rather than a raw export — write it and the "
    "highlights from what you already know about this data in the "
    "conversation, never generic filler. Its result is already shown to "
    "the user as a clear download card — never repeat its download_url "
    "or a markdown link in your reply, just describe what the document "
    "contains in one short sentence.\n"
    "- When a chart helps, call aggregate with chart set to bar (compare "
    "categories), line (trend over time), or pie (share of a whole, few "
    "categories). A grouping by month, week, day, or year is a line, even "
    "if the user only says 'gráfico' or 'agrupado por mes'. Use bar for a "
    "time series only when they explicitly ask for bars. The chat draws "
    "it, so don't paste an ASCII chart.\n"
    "- The user only sees your final reply. Do not narrate how you "
    "interpreted the question, what you plan to look up, or that you are "
    "drawing a chart. No preamble. The chat draws the chart and the data "
    "table, and links a company, client, invoice, or file when that row "
    "is one record. Don't paste that table again. Answer with the figures "
    "and at most one short conclusion.\n"
    "- A driver's profitability (rentabilidad de un chofer) is revenue "
    "minus cost. Sum work_entries total_price and cost_entries "
    "total_price filtered by that driver and company, and set them "
    "next to the employee's payroll employer_cost. asset_transactions "
    "counts the trips but has no amount. If one of those has no rows, "
    "say that side is missing instead of inventing a margin.\n"
    "- Never tally aggregate rows to produce a count, sum, or average. "
    "aggregate already returns those figures. For how many hours a person "
    "worked, sum labor_hours on production_time. Starting work opens one "
    "session per item at the same time, so duration_hours counts that "
    "overlap many times; labor_hours counts it once. For an average per "
    "day, group only by the person — not by the date — and request sum of "
    "labor_hours plus count_distinct of the date dimension (the measure is "
    "the dimension key, e.g. start_date). A worked day has labor_hours "
    "greater than 0: add that filter. Divide the two numbers the tool "
    "returns. Use duration_hours only for how long items spent in a stage.\n"
    "- Format replies in Markdown. Reply in the same language the user "
    "writes in. Be concise."
)


def _company_scope_text(companies: list) -> str:
    """Tell the model exactly which active companies (sidebar checkbox,
    not every company the user merely has access to) it is scoped to."""
    if not companies:
        return "No company is active right now; tools will return no rows."
    if len(companies) == 1:
        return (
            f"Only {companies[0].name} is active right now. Every tool call "
            "and answer is about this single company — speak about it "
            "directly, don't hedge about other companies."
        )
    names = ", ".join(company.name for company in companies)
    return (
        f"{len(companies)} companies are active right now: {names}. Tools "
        "return rows from only these; say which company a figure belongs "
        "to whenever more than one could apply."
    )


def _system_blocks(state: ConversationState, company_scope_text: str) -> list[dict]:
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT_RULES,
            "cache_control": PROMPT_CACHE_CONTROL,
        },
        {
            "type": "text",
            "text": f"## Company scope\n{company_scope_text}",
        },
        {
            "type": "text",
            "text": f"## Conversation state\n{state.to_prompt_summary()}",
        },
    ]


def _with_prompt_cache(tools: list[dict]) -> list[dict]:
    if not tools:
        return tools
    cached = [dict(tool) for tool in tools]
    cached[-1] = {**cached[-1], "cache_control": PROMPT_CACHE_CONTROL}
    return cached


def _is_user_text_message(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        for block in content
    )


def trim_history(messages: list[dict], limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Drop older turns, starting the slice on a real user question."""
    if limit < 1 or len(messages) <= limit:
        return messages
    start = len(messages) - limit
    while start > 0 and not _is_user_text_message(messages[start]):
        start -= 1
    return messages[start:]


def _dataset_label(key: str | None) -> str | None:
    if not key:
        return None
    try:
        return str(ReportDomain(key).label)
    except ValueError:
        return key


_EXPLICIT_BAR_REQUEST = re.compile(r"\bbarras?\b|\bcolumnas?\b|\bbar chart\b", re.I)


def chart_input_for_request(user_message: str, tool_input: dict) -> dict:
    """A time grouping is a line unless the user asked for bars."""
    dimensions = tool_input.get("dimensions") or []
    label = tool_input.get("chart_label_dimension")
    if not label and len(dimensions) == 1:
        label = dimensions[0]
    chart = tool_input.get("chart") or "none"
    if chart != "bar" or not is_time_bucket(label):
        return tool_input
    if _EXPLICIT_BAR_REQUEST.search(user_message or ""):
        return tool_input
    updated = dict(tool_input)
    updated["chart"] = "line"
    updated["chart_label_dimension"] = label
    return updated


def _merge_chart(current: dict | None, incoming: dict) -> dict:
    """A later listing must not wipe a chart that already has points."""

    if current and current.get("labels") and not incoming.get("labels"):
        if incoming.get("table") and not current.get("table"):
            merged = dict(current)
            merged["table"] = incoming["table"]
            return merged
        return current
    return incoming


def _result_for_model(result: dict) -> dict:
    """Drop link targets before the tool result goes back to the model."""

    data = result.get("data")
    if not isinstance(data, dict):
        return result
    cleaned = dict(data)
    cleaned.pop("table", None)
    chart = cleaned.get("chart")
    if isinstance(chart, dict):
        cleaned["chart"] = {
            key: value for key, value in chart.items() if key != "urls"
        }
    return {**result, "data": cleaned}


def _can_save_report(user, tool_calls: list) -> bool:
    if not user.has_perm("reports.use_reports"):
        return False
    return any(call.get("name") == "aggregate" for call in tool_calls or [])


def _done_event(message, text, tool_calls, chart, artifacts, user) -> dict:
    return {
        "type": "done",
        "response": text,
        "tool_calls": tool_calls,
        "chart": chart,
        "artifacts": artifacts,
        "message_id": message.pk,
        "can_save": _can_save_report(user, tool_calls),
    }


def describe_tool_call(name: str, tool_input: dict | None) -> str:
    """A short, human, Spanish status line for a tool call in progress."""
    tool_input = tool_input or {}
    dataset_label = _dataset_label(tool_input.get("dataset"))

    if name == "list_datasets":
        return "Revisando qué puedo consultar"
    if name == "describe_dataset":
        if dataset_label:
            return f"Revisando los campos de {dataset_label}"
        return "Revisando los campos disponibles"
    if name == "get_entity":
        if dataset_label:
            return f"Buscando el registro en {dataset_label}"
        return "Buscando el registro"
    if name == "traverse_relation":
        return f"Consultando {dataset_label}" if dataset_label else "Consultando relaciones"
    if name == "search_entities":
        return f"Buscando en {dataset_label}" if dataset_label else "Buscando coincidencias"
    if name == "aggregate":
        return f"Calculando totales en {dataset_label}" if dataset_label else "Calculando totales"
    if name == "generate_artifact":
        kind = str(tool_input.get("kind") or "").upper()
        return f"Generando el {kind}" if kind else "Generando el documento"
    if name == "get_bank_balances":
        return "Consultando saldos bancarios"
    if name in ("list_production_tickets", "get_production_ticket_detail", "production_summary"):
        return "Consultando producción"
    return f"Usando {name}"


def _serialize_blocks(content) -> list[dict]:
    serialized = []
    for block in content:
        if block.type == "text":
            serialized.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            serialized.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif block.type == "thinking":
            # Sonnet/Opus 5.5 think adaptively by default (no explicit
            # `thinking` param needed) and require these blocks replayed
            # verbatim, signature included, or the next call 400s.
            serialized.append(
                {
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                }
            )
        elif block.type == "redacted_thinking":
            serialized.append({"type": "redacted_thinking", "data": block.data})
        else:
            logger.warning("Unhandled content block type from Claude: %s", block.type)
            serialized.append({"type": block.type})
    return serialized


def companies_with_claude_key(user):
    """Companies the user has access to that have a Claude API key, ordered by name.

    Independent of the active-company session selection: a user can pay with
    any company's key regardless of which companies they currently have
    toggled on in the company switcher.
    """
    from core.admin.company_access import get_user_allowed_companies

    return [
        company
        for company in get_user_allowed_companies(user).order_by("name", "pk")
        if company.get_claude_api_key()
    ]


def resolve_claude_company(user, company_id=None, preferred=None):
    """Company whose key pays for this turn.

    An explicit choice wins. Otherwise the conversation's saved company,
    then the first company (of all the user has access to) that has a key.
    """
    from core.admin.company_access import get_user_allowed_companies

    available = companies_with_claude_key(user)
    by_id = {company.pk: company for company in available}
    if company_id not in (None, ""):
        try:
            chosen_id = int(company_id)
        except (TypeError, ValueError):
            chosen_id = None
        company = by_id.get(chosen_id)
        if company is not None:
            return company
        owned = get_user_allowed_companies(user).filter(pk=chosen_id).first()
        if owned is not None:
            raise RuntimeError(
                _("Set a Claude API key on %(company)s.") % {"company": owned.name}
            )
        raise RuntimeError(_("Set a Claude API key on the company."))
    if preferred is not None and preferred.pk in by_id:
        return by_id[preferred.pk]
    if available:
        return available[0]
    allowed = list(get_user_allowed_companies(user).order_by("name", "pk")[:2])
    if len(allowed) == 1:
        raise RuntimeError(
            _("Set a Claude API key on %(company)s.") % {"company": allowed[0].name}
        )
    raise RuntimeError(_("Set a Claude API key on the company."))


class ConversationService:
    """Stateless service that drives a single chat turn for one conversation."""

    def __init__(self, user, company=None):
        company = company or resolve_claude_company(user)
        api_key = company.get_claude_api_key()
        if not api_key:
            raise RuntimeError(
                _("Set a Claude API key on %(company)s.") % {"company": company.name}
            )
        self.user = user
        self.company = company
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = getattr(settings, "ANTHROPIC_AGENT_MODEL", "claude-sonnet-5-5")

    def stream_turn(self, conversation: AIConversation, user_message: str) -> Iterator[dict]:
        """Run one turn, yielding progress events as they happen.

        Event shapes: ``{"type": "step_start"|"step_done", "label": str,
        "success": bool}``, ``{"type": "text_delta", "text": str}``,
        ``{"type": "done", "response", "tool_calls", "chart", "artifacts"}``,
        ``{"type": "error", "message": str}``.
        """
        state = ConversationState.from_dict(conversation.state)
        messages = trim_history(self._load_history(conversation))

        user_content = [{"type": "text", "text": user_message}]
        messages.append({"role": "user", "content": user_content})
        AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.USER,
            content=user_content,
            display_text=user_message,
        )

        from core.admin.company_access import get_user_companies

        tools = _with_prompt_cache(ToolRegistry.get_claude_tool_definitions())
        context = ChatContext(
            user_id=conversation.owner_id, conversation_id=conversation.pk
        )
        company_scope_text = _company_scope_text(
            list(get_user_companies(self.user).order_by("name"))
        )
        tool_calls_log: list[dict] = []
        last_chart = None
        last_artifacts: list[dict] = []
        saw_thinking = False

        for _round in range(MAX_TOOL_ROUNDS):
            text_parts: list[str] = []
            pending_names: dict[int, str] = {}
            executed_results: dict[str, dict] = {}

            try:
                with self.client.messages.stream(
                    model=self.model,
                    max_tokens=4096,
                    system=_system_blocks(state, company_scope_text),
                    tools=tools,
                    messages=messages,
                    output_config={"effort": AGENT_EFFORT},
                    thinking=THINKING_BETWEEN_TOOLS,
                    cache_control=PROMPT_CACHE_CONTROL,
                ) as stream:
                    for event in stream:
                        if event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                pending_names[event.index] = block.name
                                yield {
                                    "type": "step_start",
                                    "label": describe_tool_call(block.name, None),
                                }
                            elif block.type == "thinking" and not saw_thinking:
                                saw_thinking = True
                                yield {"type": "step_start", "label": "Pensando"}
                        elif event.type == "text":
                            text_parts.append(event.text)
                        elif event.type == "content_block_stop":
                            block = event.content_block
                            if block.type == "tool_use":
                                tool_input = block.input or {}
                                if block.name == "aggregate":
                                    tool_input = chart_input_for_request(
                                        user_message, tool_input
                                    )
                                label = describe_tool_call(block.name, tool_input)
                                tool_calls_log.append(
                                    {"name": block.name, "input": tool_input}
                                )
                                result = ToolExecutor.execute(
                                    block.name, context, tool_input
                                )
                                executed_results[block.id] = result
                                yield {
                                    "type": "step_done",
                                    "label": label,
                                    "success": bool(result.get("success")),
                                }
                                chart, artifact = self._update_state(
                                    state, block.name, tool_input, result
                                )
                                if chart is not None:
                                    last_chart = _merge_chart(last_chart, chart)
                                if artifact is not None:
                                    last_artifacts.append(artifact)
                    response = stream.get_final_message()
            except Exception as exc:
                logger.exception("Claude streaming call failed")
                yield {"type": "error", "message": str(exc)}
                return

            assistant_blocks = _serialize_blocks(response.content)
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            if not tool_use_blocks:
                text = "".join(text_parts)
                for piece in text_parts:
                    yield {"type": "text_delta", "text": piece}
                self._save_state(conversation, state)
                message = AIMessage.objects.create(
                    conversation=conversation,
                    role=AIMessage.Role.ASSISTANT,
                    content=assistant_blocks,
                    display_text=text,
                    tool_calls=tool_calls_log,
                    chart=last_chart,
                    artifacts=last_artifacts,
                )
                yield _done_event(
                    message,
                    text,
                    tool_calls_log,
                    last_chart,
                    last_artifacts,
                    conversation.owner,
                )
                return

            messages.append({"role": "assistant", "content": assistant_blocks})
            AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.ASSISTANT,
                content=assistant_blocks,
                display_text="",
            )

            tool_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(
                        _result_for_model(executed_results[block.id]),
                        default=str,
                        ensure_ascii=False,
                    ),
                }
                for block in tool_use_blocks
            ]
            messages.append({"role": "user", "content": tool_results})
            AIMessage.objects.create(
                conversation=conversation,
                role=AIMessage.Role.USER,
                content=tool_results,
                display_text="",
            )

        self._save_state(conversation, state)
        fallback = (
            "I've reached the maximum number of tool calls for this turn. "
            "Please try a simpler or more specific question."
        )
        message = AIMessage.objects.create(
            conversation=conversation,
            role=AIMessage.Role.ASSISTANT,
            content=[{"type": "text", "text": fallback}],
            display_text=fallback,
            tool_calls=tool_calls_log,
            chart=last_chart,
            artifacts=last_artifacts,
        )
        yield _done_event(
            message,
            fallback,
            tool_calls_log,
            last_chart,
            last_artifacts,
            conversation.owner,
        )

    @staticmethod
    def _save_state(conversation: AIConversation, state: ConversationState) -> None:
        conversation.state = state.to_dict()
        conversation.save(update_fields=["state", "updated_at"])

    @staticmethod
    def _load_history(conversation: AIConversation) -> list[dict]:
        return [
            {"role": message.role, "content": message.content}
            for message in conversation.messages.all()
        ]

    @staticmethod
    def _update_state(
        state: ConversationState, tool_name: str, tool_input: dict, result: dict
    ) -> tuple[dict | None, dict | None]:
        """Best-effort memory update from a successful tool call.

        Returns ``(chart, artifact)`` — whichever one ``tool_name`` produced
        this call, both ``None`` otherwise.
        """
        if not result.get("success"):
            return None, None
        data = result.get("data") or {}
        tool_input = tool_input or {}

        if tool_name == "get_entity" and data.get("found"):
            entity = data.get("entity") or {}
            label = str(next(iter(entity.values()), "") or "")[:80]
            dataset = tool_input.get("dataset", "")
            entity_id = tool_input.get("id")
            if dataset and entity_id is not None:
                state.set_active(dataset, entity_id, label)

        elif tool_name == "search_entities":
            ids = [row["id"] for row in (data.get("rows") or []) if row.get("id") is not None]
            dataset = tool_input.get("dataset")
            if ids and dataset:
                state.set_focused(dataset, ids)
            table = data.get("table")
            if table:
                return {
                    "type": "none",
                    "labels": [],
                    "values": [],
                    "table": table,
                }, None
            return None, None

        elif tool_name == "traverse_relation":
            ids = [row["id"] for row in (data.get("rows") or []) if row.get("id") is not None]
            source_dataset = tool_input.get("dataset")
            relation_key = tool_input.get("relation")
            if ids and source_dataset and relation_key:
                try:
                    relation = get_dataset(source_dataset).relation_map().get(relation_key)
                except KeyError:
                    relation = None
                if relation is not None:
                    state.set_focused(relation.target_dataset, ids)

        elif tool_name == "aggregate":
            chart = data.get("chart")
            table = data.get("table")
            if not chart and not table:
                return None, None
            payload = dict(chart or {})
            if table:
                payload["table"] = table
            payload.setdefault("labels", [])
            payload.setdefault("values", [])
            payload.setdefault("type", "none")
            return payload, None

        elif tool_name == "generate_artifact":
            artifact = {
                "title": data.get("title"),
                "kind": data.get("kind"),
                "size": data.get("size"),
                "download_url": data.get("download_url"),
            }
            return None, artifact

        return None, None
