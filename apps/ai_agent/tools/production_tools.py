"""Production tools — tickets, items, and sessions.

Kept as bespoke tools (not expressed via the generic catalog primitives):
a production ticket's nested items -> stages -> sessions structure isn't a
flat catalog row, and apps.reports has no dataset for it (its
production_time dataset covers session durations, a different shape).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.ai_agent.interfaces import BaseTool, ChatContext, ToolExecutionError
from apps.ai_agent.registry import register_tool
from core.admin.company_access import get_user_companies

User = get_user_model()


def _get_user(context: ChatContext):
    try:
        return User.objects.get(pk=context.user_id)
    except User.DoesNotExist as exc:
        raise ToolExecutionError("User not found.") from exc


@register_tool
class ListProductionTickets(BaseTool):
    @property
    def name(self) -> str:
        return "list_production_tickets"

    @property
    def description(self) -> str:
        return (
            "List production tickets. Filter by status (pending, "
            "in_progress, done, cancelled). Returns ticket id, status, "
            "last stage, source object, and item count."
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
                    "enum": ["pending", "in_progress", "done", "cancelled"],
                },
                "limit": {"type": "integer", "default": 25},
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from production.models import ProductionTicket

        user = _get_user(context)
        qs = ProductionTicket.objects.filter(company__in=get_user_companies(user))

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
                "created_at": (
                    t.created_at.strftime("%d/%m/%Y %H:%M") if t.created_at else ""
                ),
            })

        return {
            "total_count": qs.count(),
            "returned_count": len(results),
            "tickets": results,
        }


@register_tool
class GetProductionTicketDetail(BaseTool):
    @property
    def name(self) -> str:
        return "get_production_ticket_detail"

    @property
    def description(self) -> str:
        return (
            "Get full detail for a production ticket: all its items, "
            "their per-stage status, and work sessions."
        )

    @property
    def required_permission(self) -> str | None:
        return "production.view_productionticket"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"ticket_id": {"type": "integer"}},
            "required": ["ticket_id"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from production.models import ProductionTicket

        user = _get_user(context)
        try:
            ticket = ProductionTicket.objects.get(
                pk=params["ticket_id"], company__in=get_user_companies(user)
            )
        except ProductionTicket.DoesNotExist as exc:
            raise ToolExecutionError(
                f"Production ticket #{params['ticket_id']} not found."
            ) from exc

        source = ticket.source_object
        items_data = []
        for item in ticket.items.select_related("component").all():
            stages = [
                {
                    "stage": stage_name,
                    "status": (info or {}).get("status", "pending"),
                    "duration_minutes": (info or {}).get("duration_minutes"),
                }
                for stage_name, info in item.get_ordered_planning()
            ]
            sessions = [
                {
                    "stage": s.production_stage,
                    "status": s.get_status_display(),
                    "start_time": (
                        s.start_time.strftime("%d/%m/%Y %H:%M") if s.start_time else ""
                    ),
                    "end_time": (
                        s.end_time.strftime("%d/%m/%Y %H:%M") if s.end_time else ""
                    ),
                }
                for s in item.sessions.order_by("start_time")
            ]
            items_data.append({
                "id": item.id,
                "component": str(item.component) if item.component else "",
                "quantity": str(item.quantity) if item.quantity else "",
                "status": item.get_status_display(),
                "stages": stages,
                "sessions": sessions,
            })

        return {
            "id": ticket.id,
            "status": ticket.get_status_display(),
            "last_stage": ticket.last_stage_label or "",
            "source": str(source) if source else "",
            "items": items_data,
        }


@register_tool
class ProductionSummary(BaseTool):
    @property
    def name(self) -> str:
        return "production_summary"

    @property
    def description(self) -> str:
        return (
            "High-level production summary: ticket counts by status and "
            "the current stage distribution."
        )

    @property
    def required_permission(self) -> str | None:
        return "production.view_productionticket"

    def get_parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, context: ChatContext, params: dict) -> dict:
        from django.db.models import Count

        from production.models import ProductionTicket

        user = _get_user(context)
        qs = ProductionTicket.objects.filter(company__in=get_user_companies(user))

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
