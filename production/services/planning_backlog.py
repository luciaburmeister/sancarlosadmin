"""Tickets with one or more product stages still waiting for the calendar.

A ticket stays in the backlog until every stage has a session, so stages can
be placed independently, including on different days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db.models import F, OuterRef, Prefetch, Subquery
from django.utils import timezone

from company.models import Employee, Machine
from configurator.models import (
    Configuration,
    ConfigurationItem,
    ConfigurationParameterValue,
)
from production.models import (
    PlannedProductionSession,
    ProductionResource,
    ProductionTicket,
    ProductionTicketStatus,
)
from production.services.planning_stages import resolve_ticket_stages

DEFAULT_LIMIT = 50
BATCH_SIZE = 50

URGENCY_OVERDUE = "overdue"
URGENCY_SOON = "soon"
URGENCY_LATER = "later"
URGENCY_NONE = "none"

SOON_DAYS = 7


@dataclass
class BacklogItem:
    ticket_id: int
    label: str
    product: str
    delivery_date: date | None
    urgency: str
    total_minutes: int
    stage_count: int
    stages: list[dict]

    @property
    def hours_label(self) -> str:
        hours, minutes = divmod(self.total_minutes, 60)
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"

    @property
    def needs_time_configuration(self) -> bool:
        return any(
            stage["total_time_min"] <= 0 and not stage["is_planned"]
            for stage in self.stages
        )

    def as_dict(self) -> dict:
        """The small, explicit payload used by the calendar's planning panel."""
        return {
            "ticket_id": self.ticket_id,
            "label": self.label,
            "product": self.product,
            "total_minutes": self.total_minutes,
            "stage_count": self.stage_count,
            "needs_time_configuration": self.needs_time_configuration,
            "planned_stage_count": sum(stage["is_planned"] for stage in self.stages),
            "stages": self.stages,
        }


def _urgency(delivery_date: date | None, today: date) -> str:
    if delivery_date is None:
        return URGENCY_NONE
    if delivery_date < today:
        return URGENCY_OVERDUE
    if delivery_date <= today + timedelta(days=SOON_DAYS):
        return URGENCY_SOON
    return URGENCY_LATER


def _candidate_tickets(
    company, *, offset: int = 0, batch_size: int = BATCH_SIZE
) -> list[ProductionTicket]:
    """Load one delivery-ordered batch with the data needed to check eligibility."""
    configuration_ct = ContentType.objects.get_for_model(Configuration)
    delivery_date = Configuration.objects.filter(
        pk=OuterRef("object_id")
    ).values("quote__estimated_delivery_date")[:1]

    # A partly-planned ticket needs its session stage names, not just planning_status.
    return list(
        ProductionTicket.objects.filter(
            company=company,
            status__in=[
                ProductionTicketStatus.PENDING,
                ProductionTicketStatus.IN_PROGRESS,
            ],
            content_type=configuration_ct,
        )
        .annotate(_delivery_date=Subquery(delivery_date))
        .order_by(F("_delivery_date").asc(nulls_last=True), "pk")
        .select_related("project_file__client")
        .prefetch_related(
            GenericPrefetch(
                "source_object",
                [
                    Configuration.objects.select_related(
                        "product", "quote__project_file__client"
                    ).prefetch_related(
                        Prefetch(
                            "items",
                            queryset=ConfigurationItem.objects.select_related(
                                "component"
                            ),
                        ),
                        "product__parameters",
                        "product__computed_vars",
                        Prefetch(
                            "param_values",
                            queryset=ConfigurationParameterValue.objects.select_related(
                                "parameter"
                            ),
                        ),
                    )
                ],
            ),
            Prefetch(
                "planned_sessions",
                queryset=PlannedProductionSession.objects.only(
                    "id", "production_ticket_id", "production_stage"
                ),
            ),
        )[offset : offset + batch_size]
    )


def unplanned_tickets(company, *, limit: int = DEFAULT_LIMIT) -> list[BacklogItem]:
    if limit <= 0:
        return []

    today = timezone.localdate()
    items = []
    offset = 0
    while len(items) < limit:
        tickets = _candidate_tickets(company, offset=offset, batch_size=BATCH_SIZE)
        if not tickets:
            break
        offset += len(tickets)
        for ticket in tickets:
            configuration = ticket.source_object
            delivery_date = ticket._delivery_date
            planned_stage_names = {
                session.production_stage for session in ticket.planned_sessions.all()
            }
            product_stages = list(
                getattr(getattr(configuration, "product", None), "production_stages", [])
                or []
            )
            # Skip duration resolution for a ticket that is already fully planned.
            if product_stages and all(stage in planned_stage_names for stage in product_stages):
                continue

            stages = resolve_ticket_stages(ticket, include_unconfigured=True)
            # A ticket with no resolvable stages can't be scheduled; skip its card.
            if not stages:
                continue
            for stage in stages:
                stage["is_planned"] = stage["stage"] in planned_stage_names
            if all(stage["is_planned"] for stage in stages):
                continue

            items.append(
                BacklogItem(
                    ticket_id=ticket.pk,
                    label=ticket.display_label,
                    product=str(getattr(configuration, "product", "") or ""),
                    delivery_date=delivery_date,
                    urgency=_urgency(delivery_date, today),
                    total_minutes=sum(
                        stage["total_time_min"]
                        for stage in stages
                        if not stage["is_planned"]
                    ),
                    stage_count=len(stages),
                    stages=stages,
                )
            )
            if len(items) == limit:
                break
        if len(tickets) < BATCH_SIZE:
            break

    compatible_resources = _compatible_resources(
        company,
        {
            stage["stage"]
            for item in items
            for stage in item.stages
            if not stage["is_planned"]
        },
    )
    for item in items:
        for stage in item.stages:
            stage["compatible_resource_ids"] = compatible_resources.get(
                stage["stage"], []
            )
    return items


def _compatible_resources(company, stages: set[str]) -> dict[str, list[int]]:
    """Active resources allowed to receive each stage on a calendar drop."""
    compatible = {stage: [] for stage in stages}
    if not stages:
        return compatible

    resources = ProductionResource.objects.filter(
        company=company, is_active=True
    ).prefetch_related(
        GenericPrefetch("target", [Employee.objects.all(), Machine.objects.all()])
    )
    for resource in resources:
        target = resource.target
        if target is None:
            continue
        if isinstance(target, Employee) and not target.can_work_on_production:
            continue
        allowed = set(getattr(target, "production_stages", None) or [])
        for stage in stages:
            if stage in allowed:
                compatible[stage].append(resource.pk)
    return compatible
