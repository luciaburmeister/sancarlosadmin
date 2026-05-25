"""Cancel production tickets and related stage sessions."""

import unicodedata

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from configurator.models import Configuration
from production.models import (
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
    ProductionTicket,
    ProductionTicketStatus,
)
from production.services.stages_to_sessions import StagesToSessionsService


def _close_all_open_sessions(item: ProductionItem) -> None:
    now = timezone.now()
    qs = ProductionItemSession.objects.filter(
        production_item=item,
        end_time__isnull=True,
    )
    for sess in qs:
        sess.end_time = now
        sess.status = ProductionItemSessionStatus.CANCELLED
        sess.save(update_fields=["end_time", "status", "updated_at"])


def _cancel_item_planning(item: ProductionItem) -> None:
    planning = {
        unicodedata.normalize("NFC", k): v
        for k, v in (item.planning or {}).items()
    }
    for stage_name, info in list(planning.items()):
        stage_info = dict(info or {})
        stage_info["status"] = ProductionItemStatus.CANCELLED
        planning[stage_name] = stage_info
    item.planning = planning
    item.status = ProductionItemStatus.CANCELLED
    item.save(update_fields=["planning", "status", "updated_at"])


def cancel_production_ticket(ticket: ProductionTicket) -> bool:
    """Cancel a ticket, its items' stages, and open sessions. Returns True if changed."""
    if ticket.status in (
        ProductionTicketStatus.DONE,
        ProductionTicketStatus.CANCELLED,
    ):
        return False

    items = list(ProductionItem.objects.filter(ticket_id=ticket.pk))
    for item in items:
        for stage_name in list((item.planning or {}).keys()):
            StagesToSessionsService._close_open_sessions_for_stage(
                item,
                unicodedata.normalize("NFC", stage_name),
            )
        _close_all_open_sessions(item)
        _cancel_item_planning(item)

    ProductionTicket.objects.filter(pk=ticket.pk).update(
        status=ProductionTicketStatus.CANCELLED,
    )
    ticket.status = ProductionTicketStatus.CANCELLED
    return True


def cancel_tickets_for_project_file(project_file) -> int:
    """Cancel all active production tickets linked to configurations on this expediente."""
    config_ids = []
    for quote in project_file.quotes.all():
        config_ids.extend(quote.configurations.values_list("id", flat=True))
    if not config_ids:
        return 0

    ct = ContentType.objects.get_for_model(Configuration)
    tickets = ProductionTicket.objects.filter(
        content_type=ct,
        object_id__in=config_ids,
        company_id=project_file.company_id,
    ).exclude(
        status__in=(
            ProductionTicketStatus.DONE,
            ProductionTicketStatus.CANCELLED,
        ),
    )

    count = 0
    for ticket in tickets:
        if cancel_production_ticket(ticket):
            count += 1
    return count
