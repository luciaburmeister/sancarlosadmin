"""Derived delivery / production status for ProjectFile (expediente).

Both statuses are denormalized onto the ProjectFile and recomputed from the
delivery orders and production tickets that belong to it, following the same
``compute_* / recompute_*`` split used by ``sales.services.quote_production_status``.

The lifecycle ``status`` reacts to them forward-only:

* ``new`` -> ``active`` as soon as production starts or anything is shipped.
* ``active`` -> ``completed`` when everything is delivered, production is done
  (or there is none) and the expediente is fully invoiced.

``on_hold`` and ``cancelled`` are never touched, and nothing moves backwards:
a manual ``completed`` on a service-only expediente must survive.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q, Sum


def _delivery_counting_statuses():
    from sales.models import DeliveryOrderStatus

    return [DeliveryOrderStatus.SENT, DeliveryOrderStatus.DELIVERED]


def get_ordered_quantities(project_file):
    """{(content_type_id, object_id): ordered_quantity} for every deliverable
    source line of the expediente: configurations of accepted quotes, the
    quotes' inventory lines and direct (shop / work order) configurations."""
    from configurator.models import Configuration, ConfigurationStatus
    from sales.models import Quote, QuoteInventoryItem, QuoteStatus

    config_ct = ContentType.objects.get_for_model(Configuration)
    inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)
    ordered = {}

    accepted_quote_ids = Quote.objects.filter(
        project_file=project_file, status=QuoteStatus.ACCEPTED,
    ).values_list("id", flat=True)

    configs = Configuration.objects.filter(
        Q(quote_id__in=accepted_quote_ids)
        | Q(
            project_file=project_file,
            quote__isnull=True,
            status__in=[
                ConfigurationStatus.SHOP,
                ConfigurationStatus.COMPLETED,
                ConfigurationStatus.CONVERTED,
                ConfigurationStatus.PRODUCTION_DONE,
            ],
        )
    ).values_list("id", "units")
    for config_id, units in configs:
        ordered[(config_ct.id, config_id)] = units if units and units > 0 else Decimal("1")

    inv_items = QuoteInventoryItem.objects.filter(
        quote_id__in=accepted_quote_ids,
    ).values_list("id", "quantity")
    for item_id, quantity in inv_items:
        ordered[(inv_item_ct.id, item_id)] = quantity or Decimal("0")

    return ordered


def get_shipped_quantities(project_file):
    """{(content_type_id, object_id): shipped_quantity} from delivery orders
    that actually left (sent or delivered). Drafts are not deliveries and
    cancelled orders never count. Lines without a source are keyed by None."""
    from sales.models import DeliveryOrderItem

    rows = (
        DeliveryOrderItem.objects.filter(
            delivery_order__project_file=project_file,
            delivery_order__status__in=_delivery_counting_statuses(),
        )
        .values("source_content_type_id", "source_object_id")
        .annotate(total=Sum("quantity"))
    )
    shipped = {}
    for row in rows:
        if row["source_content_type_id"] is None:
            key = None
        else:
            key = (row["source_content_type_id"], row["source_object_id"])
        shipped[key] = shipped.get(key, Decimal("0")) + (row["total"] or Decimal("0"))
    return shipped


def compute_delivery_status(project_file):
    from management.models import ProjectFileDeliveryStatus

    ordered = get_ordered_quantities(project_file)
    shipped = get_shipped_quantities(project_file)
    free_lines_shipped = shipped.pop(None, Decimal("0"))
    anything_shipped = free_lines_shipped > 0 or any(q > 0 for q in shipped.values())

    if not ordered:
        if anything_shipped:
            return ProjectFileDeliveryStatus.DELIVERED
        return ProjectFileDeliveryStatus.NOT_APPLICABLE

    if not anything_shipped:
        return ProjectFileDeliveryStatus.NOT_DELIVERED

    fully = all(
        shipped.get(key, Decimal("0")) >= qty for key, qty in ordered.items()
    )
    if fully:
        return ProjectFileDeliveryStatus.DELIVERED
    return ProjectFileDeliveryStatus.PARTIALLY_DELIVERED


def production_tickets_for(project_file):
    """Non-cancelled tickets of the expediente, including legacy tickets that
    only reach it through their configuration's quote."""
    from configurator.models import Configuration
    from production.models import ProductionTicket, ProductionTicketStatus

    config_ct = ContentType.objects.get_for_model(Configuration)
    fallback_config_ids = Configuration.objects.filter(
        quote__project_file=project_file,
    ).values_list("id", flat=True)
    return ProductionTicket.objects.filter(
        Q(project_file=project_file)
        | Q(project_file__isnull=True, content_type=config_ct, object_id__in=fallback_config_ids)
    ).exclude(status=ProductionTicketStatus.CANCELLED)


def compute_production_status(project_file):
    from management.models import ProjectFileProductionStatus
    from production.models import ProductionTicketStatus

    statuses = list(production_tickets_for(project_file).values_list("status", flat=True))
    if not statuses:
        return ProjectFileProductionStatus.NO_PRODUCTION
    if all(s == ProductionTicketStatus.DONE for s in statuses):
        return ProjectFileProductionStatus.DONE
    if any(s in (ProductionTicketStatus.IN_PROGRESS, ProductionTicketStatus.DONE) for s in statuses):
        return ProjectFileProductionStatus.IN_PROGRESS
    return ProjectFileProductionStatus.PENDING


def compute_lifecycle_status(project_file, delivery_status, production_status):
    """Forward-only lifecycle transition implied by the derived statuses.
    Returns the current status when nothing should change."""
    from management.models import (
        ProjectFileDeliveryStatus,
        ProjectFileProductionStatus,
        ProjectFileStatus,
    )

    current = project_file.status
    shipping_started = delivery_status in (
        ProjectFileDeliveryStatus.PARTIALLY_DELIVERED,
        ProjectFileDeliveryStatus.DELIVERED,
    )
    production_started = production_status in (
        ProjectFileProductionStatus.IN_PROGRESS,
        ProjectFileProductionStatus.DONE,
    )

    if current == ProjectFileStatus.NEW and (shipping_started or production_started):
        return ProjectFileStatus.ACTIVE

    if current == ProjectFileStatus.ACTIVE:
        delivered = delivery_status == ProjectFileDeliveryStatus.DELIVERED
        production_done = production_status in (
            ProjectFileProductionStatus.DONE,
            ProjectFileProductionStatus.NO_PRODUCTION,
        )
        if delivered and production_done and project_file.is_fully_invoiced:
            return ProjectFileStatus.COMPLETED

    return current


def recompute_project_file_status(project_file_id):
    """Refresh the denormalized delivery/production statuses of one expediente
    and apply the lifecycle transition they imply. Safe to call from signals:
    writes through ``update()`` so no ``save()`` side effects fire, and skips
    the write when nothing changed."""
    from management.models import ProjectFile

    if not project_file_id:
        return None
    project_file = ProjectFile.objects.filter(pk=project_file_id).first()
    if project_file is None:
        return None

    delivery_status = compute_delivery_status(project_file)
    production_status = compute_production_status(project_file)
    lifecycle = compute_lifecycle_status(project_file, delivery_status, production_status)

    changes = {}
    if delivery_status != project_file.delivery_status:
        changes["delivery_status"] = delivery_status
    if production_status != project_file.production_status:
        changes["production_status"] = production_status
    if lifecycle != project_file.status:
        changes["status"] = lifecycle
    if changes:
        ProjectFile.objects.filter(pk=project_file_id).update(**changes)
        for field, value in changes.items():
            setattr(project_file, field, value)
    return project_file
