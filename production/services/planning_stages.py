"""Resolve a ticket's stages into durations, quantities and resource pools.

Delegates to the same code the optimizer uses, so the two can't disagree. A
stage's duration is the **max** across its items, not the sum -- summing would
quietly diverge from every optimizer run.
"""

from __future__ import annotations

from configurator.models import Configuration
from production.services.scheduler.data import _resolve_stage_info
from production.services.session_scheduler import get_resources_for_stage


def resolve_ticket_stages(ticket, *, include_unconfigured: bool = False) -> list[dict]:
    """Stages of *ticket* in its effective order, with duration and quantities.

    Stages with no configured time are dropped unless ``include_unconfigured``,
    which the calendar uses to let a planner enter ticket-specific times.
    """
    configuration = ticket.source_object
    if not isinstance(configuration, Configuration):
        return []

    product = getattr(configuration, "product", None)

    # Filter in Python to keep the backlog's prefetched items query flat-cost.
    items = [item for item in configuration.items.all() if item.is_template_item]

    # The stages a ticket actually works are the ones its BOM items declare --
    # the same source ConfigurationToProductionService, SessionScheduler and the
    # optimizer all read. The product's own list is only an ordering hint: a
    # stage no item passes through is not work this order performs.
    covered = {stage for item in items for stage in (item.production_stages or [])}
    if not covered:
        return []

    product_stage_order = list(getattr(product, "production_stages", None) or [])
    # A ticket-specific reorder applies wherever stages are resolved from here.
    stage_order = [
        stage for stage in ticket.get_stage_order() if stage in covered
    ]
    stage_order.extend(
        stage
        for stage in product_stage_order
        if stage in covered and stage not in stage_order
    )
    # An item stage the product's list never mentions still belongs to the order.
    stage_order.extend(
        stage for stage in sorted(covered) if stage not in stage_order
    )

    durations: dict[str, int] = {}
    machine_qty: dict[str, int] = {}
    employee_qty: dict[str, int] = {}
    resource_requirements_configured: set[str] = set()

    # An item's stage_resources is a snapshot taken when its BOM was built, so
    # it is empty for anything configured on the template afterwards. Reading
    # the template for those stages keeps the board in step with it the way
    # resync_planning_from_template() keeps the optimizer in step; a snapshot
    # that does hold values still wins, so a deliberate per-order time stands.
    template_resources = getattr(product, "production_planning_resources", None) or {}

    # Filter in Python to keep the backlog's prefetched items query flat-cost.
    items = (
        item for item in configuration.items.all() if item.is_template_item
    )
    context = None
    for item in items:
        stage_resources = item.stage_resources or {}

        for stage in (item.production_stages or []):
            raw = stage_resources.get(stage) or template_resources.get(stage)
            if not raw:
                continue

            if any(key in raw for key in (
                "machine_qty", "employee_qty", "machine_types", "employee_types",
            )):
                resource_requirements_configured.add(stage)

            if context is None:
                context = configuration.get_pricing_context()
            resolved = _resolve_stage_info(
                raw, context, stage=stage, configuration=configuration
            )
            durations[stage] = max(durations.get(stage, 0), resolved["total_time_min"])
            machine_qty[stage] = max(machine_qty.get(stage, 0), resolved["machine_qty"])
            employee_qty[stage] = max(employee_qty.get(stage, 0), resolved["employee_qty"])

    return [
        {
            "stage": stage,
            "total_time_min": durations.get(stage, 0),
            "machine_qty": machine_qty.get(stage, 0),
            "employee_qty": employee_qty.get(stage, 0),
            "resource_requirements_configured": stage in resource_requirements_configured,
        }
        for stage in stage_order
        if include_unconfigured or durations.get(stage, 0) > 0
    ]


def ticket_stage_plan(ticket, *, include_unconfigured: bool = False) -> list[dict]:
    """:func:`resolve_ticket_stages` plus the candidate resources per stage."""
    stages = resolve_ticket_stages(
        ticket, include_unconfigured=include_unconfigured
    )

    for stage in stages:
        pools = get_resources_for_stage(
            stage["stage"],
            {
                "machine_qty": stage["machine_qty"],
                "employee_qty": stage["employee_qty"],
            },
            company=ticket.company,
        )
        stage["machine_pool"] = pools["machines"]
        stage["employee_pool"] = pools["employees"]

    return stages


def ticket_total_minutes(ticket) -> int:
    """Grid the ticket will occupy end to end, for the backlog card."""
    return sum(stage["total_time_min"] for stage in resolve_ticket_stages(ticket))
