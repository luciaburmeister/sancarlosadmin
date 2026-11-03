"""Shared config/quote/expediente label-building, spanning ProductionTicket,
Configuration and Quote/ProjectFile — used by several terminal endpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TicketLabels:
    config_id: int | None
    config_name: str
    config_notes: str
    quote_name: str
    template_name: str | None
    ticket_status: str
    config_created_at: str
    expediente: dict | None


def build_ticket_labels(*, ticket, config, project_file) -> TicketLabels:
    """Build the label set for one ticket; *config*/*project_file* are passed
    in (not re-resolved) so prefetched callers don't pay for it twice."""
    quote = getattr(config, "quote", None)
    quote_name = (
        (quote.project_name or "").strip()
        if quote and (quote.project_name or "").strip()
        else (getattr(config, "name", None) or "")
    )

    expediente = None
    if project_file:
        start_date = project_file.start_date
        expediente = {
            "id": project_file.id,
            "reference": getattr(project_file, "reference", None) or "",
            "name": project_file.name or "",
            "client_name": project_file.client.name if project_file.client else None,
            "start_date": (
                start_date.strftime("%Y-%m-%d")
                if hasattr(start_date, "strftime")
                else start_date
            ),
            "is_standing_task_pool": project_file.is_standing_task_pool,
        }

    return TicketLabels(
        config_id=config.id if config else None,
        config_name=getattr(config, "name", None) or "",
        config_notes=getattr(config, "notes", None) or "",
        quote_name=quote_name,
        template_name=config.product.name if getattr(config, "product", None) else None,
        ticket_status=ticket.status,
        config_created_at=(config.created_at if config else ticket.created_at).date().isoformat(),
        expediente=expediente,
    )
