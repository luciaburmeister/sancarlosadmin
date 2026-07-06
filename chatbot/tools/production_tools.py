"""
Production tools — Tickets, Items, and Sessions.
"""

from __future__ import annotations

from chatbot.interfaces import BaseTool, ChatContext, ToolExecutionError
from chatbot.registry import register_tool


@register_tool
class ListProductionTickets(BaseTool):
    """List production tickets with status filter."""

    @property
    def name(self) -> str:
        return "list_production_tickets"

    @property
    def description(self) -> str:
        return (
            "List production tickets for the company. "
            "Filter by status (pending, in_progress, done, cancelled). "
            "Returns ticket ID, status, last stage, source object, and item count."
        )

    @property
    def required_permission(self) -> str | None:
        return "production.view_productionticket"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by ticket status.",
                    "enum": ["pending", "in_progress", "done", "cancelled"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results.",
                    "default": 25,
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from production.models import ProductionTicket

        qs = ProductionTicket.objects.filter(company_id=context.company_id)

        if status := params.get("status"):
            qs = qs.filter(status=status)

        limit = min(params.get("limit", 25), 100)
        tickets = qs.order_by("-created_at")[:limit]

        results = []
        for t in tickets:
            source = t.source_object
            source_label = str(source) if source else f"Object #{t.object_id}"
            results.append({
                "id": t.id,
                "status": t.get_status_display(),
                "last_stage": t.last_stage_label or "",
                "source": source_label,
                "items_count": t.items.count(),
                "created_at": t.created_at.strftime("%d/%m/%Y %H:%M") if t.created_at else "",
            })

        return {
            "total_count": qs.count(),
            "returned_count": len(results),
            "tickets": results,
        }


@register_tool
class GetProductionTicketDetail(BaseTool):
    """Get full production ticket detail with items and sessions."""

    @property
    def name(self) -> str:
        return "get_production_ticket_detail"

    @property
    def description(self) -> str:
        return (
            "Get detailed information for a production ticket, including "
            "all production items, their stages, statuses, and work sessions."
        )

    @property
    def required_permission(self) -> str | None:
        return "production.view_productionticket"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "integer",
                    "description": "The ID of the production ticket.",
                },
            },
            "required": ["ticket_id"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from production.models import ProductionTicket

        try:
            ticket = ProductionTicket.objects.get(
                pk=params["ticket_id"], company_id=context.company_id
            )
        except ProductionTicket.DoesNotExist:
            raise ToolExecutionError(f"Production ticket #{params['ticket_id']} not found.")

        source = ticket.source_object
        stage_order = ticket.get_stage_order()

        items_data = []
        for item in ticket.items.select_related("component").all():
            # Build per-stage status from planning JSON
            stages = []
            for stage_name, info in item.get_ordered_planning():
                info = info or {}
                stages.append({
                    "stage": stage_name,
                    "status": info.get("status", "pending"),
                    "duration_minutes": info.get("duration_minutes"),
                })

            sessions = [
                {
                    "stage": s.production_stage,
                    "status": s.get_status_display(),
                    "start_time": s.start_time.strftime("%d/%m/%Y %H:%M") if s.start_time else "",
                    "end_time": s.end_time.strftime("%d/%m/%Y %H:%M") if s.end_time else "",
                    "created_by": str(s.created_by) if s.created_by else "",
                    "completed_by": str(s.completed_by) if s.completed_by else "",
                    "comments": [
                        e.get("text", "")
                        for e in (s.comment_entries or [])
                        if isinstance(e, dict)
                    ],
                }
                for s in item.sessions.select_related("created_by", "completed_by").order_by("start_time")
            ]

            items_data.append({
                "id": item.id,
                "component": str(item.component) if item.component else "",
                "quantity": str(item.quantity) if item.quantity else "",
                "length": str(item.length) if item.length else "",
                "status": item.get_status_display(),
                "stages": stages,
                "sessions": sessions,
                "data": item.data or {},
            })

        return {
            "id": ticket.id,
            "status": ticket.get_status_display(),
            "last_stage": ticket.last_stage_label or "",
            "source": str(source) if source else "",
            "stage_order": stage_order,
            "created_at": ticket.created_at.strftime("%d/%m/%Y %H:%M") if ticket.created_at else "",
            "items": items_data,
        }


@register_tool
class ProductionSummary(BaseTool):
    """Get a high-level production summary."""

    @property
    def name(self) -> str:
        return "production_summary"

    @property
    def description(self) -> str:
        return (
            "Get a high-level summary of production status: "
            "counts of tickets by status (pending, in_progress, done), "
            "and the current stage distribution."
        )

    @property
    def required_permission(self) -> str | None:
        return "production.view_productionticket"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from django.db.models import Count
        from production.models import ProductionTicket

        qs = ProductionTicket.objects.filter(company_id=context.company_id)

        status_counts = dict(
            qs.values_list("status")
            .annotate(count=Count("id"))
            .values_list("status", "count")
        )

        stage_counts = dict(
            qs.exclude(last_stage_label__isnull=True)
            .exclude(last_stage_label="")
            .values_list("last_stage_label")
            .annotate(count=Count("id"))
            .values_list("last_stage_label", "count")
        )

        return {
            "total_tickets": qs.count(),
            "by_status": status_counts,
            "by_current_stage": stage_counts,
        }


@register_tool
class GetProductionStageVideo(BaseTool):
    """Return a demo video for a production stage."""

    @property
    def name(self) -> str:
        return "get_production_stage_video"

    @property
    def description(self) -> str:
        return (
            "Get a video showing a production stage in action. "
            "Use this when the user asks to see a video or visual demonstration "
            "of any production stage or process. Returns a YouTube link that "
            "should be embedded in the response."
        )

    @property
    def required_permission(self) -> str | None:
        return "production.view_productionticket"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "stage_name": {
                    "type": "string",
                    "description": "The production stage to show a video for (e.g. 'cutting', 'assembly').",
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        stage_name = params.get("stage_name", "general")

        return {
            "stage": stage_name,
            "video_url": "https://www.youtube.com/watch?v=4KfdmQJx3to",
            "title": f"Production stage video: {stage_name}",
            "instructions": (
                "Share this YouTube link with the user. "
                "Format it as a clickable markdown link which opens on a new tab."
            ),
        }
