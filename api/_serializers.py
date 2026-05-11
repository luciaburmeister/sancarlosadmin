"""Shared serializers and lookup helpers for the API views."""

import unicodedata

from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse

from configurator.models import Configuration
from production.models import ProductionTicket


# ─── Project files (a.k.a. expedientes) ───────────────────────────────────────


def serialize_project_file(project_file, include_total=False):
    start_date = project_file.start_date
    data = {
        "id": project_file.id,
        "reference": project_file.reference,
        "name": project_file.name,
        "client_id": project_file.client_id,
        "client": project_file.client.name if project_file.client else None,
        "status": project_file.status,
        "start_date": start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else start_date,
        "created_at": project_file.created_at.strftime("%Y-%m-%d %H:%M"),
    }
    if include_total:
        quotes = list(project_file.quotes.all())
        data["quote_count"] = len(quotes)
        data["total_price"] = round(
            sum(float(q.total_price.amount) for q in quotes if q.total_price), 2
        )
    return data


# ─── Presupuestos (quotes) ────────────────────────────────────────────────────


def serialize_product_config(config):
    """Client-facing configuration: shows price to client, hides internal costs."""
    needs_assignment = _config_needs_assignment(config)
    pricing_breakdown = [
        {"label": line.get("label", ""), "amount": float(line.get("amount", 0))}
        for line in (config.pricing_breakdown or [])
        if isinstance(line, dict) and line.get("show_in_export")
    ]
    return {
        "id": config.id,
        "name": config.name,
        "status": config.status,
        "template_id": config.product_id,
        "template_name": config.product.name if config.product else None,
        "total_price": float(config.total_price.amount) if config.total_price else None,
        "price_override": float(config.price_override.amount) if config.price_override else None,
        "param_values": {
            pv.parameter.name: pv.value
            for pv in config.param_values.select_related("parameter").all()
        },
        "param_labels": {
            pv.parameter.name: pv.parameter.label or pv.parameter.name
            for pv in config.param_values.select_related("parameter").all()
        },
        "needs_assignment": needs_assignment,
        "pricing_breakdown": pricing_breakdown,
        "created_at": config.created_at.strftime("%Y-%m-%d") if config.created_at else None,
    }


def serialize_presupuesto(quote, include_products=True):
    from invoices.models import SalesInvoice
    try:
        client_name = (
            quote.project_file.client.name
            if quote.project_file and quote.project_file.client
            else None
        )
    except Exception:
        client_name = None

    data = {
        "id": quote.id,
        "name": quote.project_name,
        "status": quote.status,
        "created_at": quote.created_at.strftime("%Y-%m-%d %H:%M"),
        "expires_at": quote.expires_at.strftime("%Y-%m-%d") if quote.expires_at else None,
        "total_price": float(quote.total_price.amount) if quote.total_price else 0.0,
        "client_name": client_name,
    }
    if include_products:
        configs = list(
            quote.configurations
            .select_related("product")
            .prefetch_related("param_values__parameter")
            .all()
        )
        data["product_count"] = len(configs)
        data["products"] = [serialize_product_config(c) for c in configs]
        config_ids = [c.id for c in configs]
        invoiced_count = SalesInvoice.objects.filter(
            configuration_id__in=config_ids
        ).values("configuration_id").distinct().count()
        data["is_invoiced"] = bool(config_ids) and invoiced_count >= len(config_ids)
    else:
        data["product_count"] = quote.configurations.count()
        data["is_invoiced"] = False
    return data


# ─── Configurations ───────────────────────────────────────────────────────────


def serialize_configuration(config):
    needs_assignment = _config_needs_assignment(config)
    items = list(config.items.select_related("component").all())
    items_data = [
        {
            "id": it.id,
            "name": it.component.name if it.component else "—",
            "quantity": float(it.quantity or 0),
            "unit_price": float(it.unit_price.amount) if it.unit_price else 0.0,
            "line_total": float(it.line_total.amount) if it.line_total else 0.0,
        }
        for it in items
    ]
    return {
        "id": config.id,
        "name": config.name,
        "status": config.status,
        "template_id": config.product_id,
        "template_name": config.product.name if config.product else None,
        "price_override": float(config.price_override.amount) if config.price_override else None,
        "total_price": float(config.total_price.amount) if config.total_price else None,
        "subtotal_price": float(config.subtotal_price.amount) if config.subtotal_price else None,
        "items": items_data,
        "param_values": {
            pv.parameter.name: pv.value
            for pv in config.param_values.select_related("parameter").all()
        },
        "param_labels": {
            pv.parameter.name: pv.parameter.label or pv.parameter.name
            for pv in config.param_values.select_related("parameter").all()
        },
        "needs_assignment": needs_assignment,
        "created_at": config.created_at.strftime("%Y-%m-%d") if config.created_at else None,
    }


# ─── Clients ──────────────────────────────────────────────────────────────────


def serialize_client(c):
    return {
        "id": c.id,
        "name": c.name,
        "phone": c.phone or "",
        "email": c.email or "",
        "payment_method": c.payment_method or "transfer",
    }


# ─── Production tickets and stages ────────────────────────────────────────────


def _config_needs_assignment(config):
    """
    Returns True when a production config has at least one pending/in-progress
    stage without any employee assigned to its item.
    """
    if config.status not in {"converted", "shop"}:
        return False

    ct = ContentType.objects.get_for_model(config)
    ticket = (
        ProductionTicket.objects.prefetch_related("items__assigned_employees")
        .filter(content_type=ct, object_id=config.id)
        .first()
    )
    if ticket is None:
        return False

    items = active_ticket_items(ticket)
    for item in items:
        has_assigned = bool(list(item.assigned_employees.all()))
        for info in (item.planning or {}).values():
            status = (info or {}).get("status") or "pending"
            if status in {"pending", "in_progress"} and not has_assigned:
                return True
    return False


def get_ticket_for_config(config_id, company):
    """Return (config, ticket, error_response). ticket may be None if not yet converted."""
    try:
        config = Configuration.objects.get(id=config_id, company=company)
    except Configuration.DoesNotExist:
        return None, None, JsonResponse({"error": "Not found"}, status=404)

    ct = ContentType.objects.get_for_model(config)
    try:
        ticket = ProductionTicket.objects.prefetch_related(
            "items__assigned_employees"
        ).get(content_type=ct, object_id=config.id)
    except ProductionTicket.DoesNotExist:
        return config, None, None

    return config, ticket, None


def active_ticket_items(ticket):
    """Return ProductionItems that still belong to the current configuration BOM.

    If the ticket source is not a Configuration (or cannot be resolved), return all
    ticket items to preserve backward-compatible behavior.
    """
    items = list(ticket.items.prefetch_related("assigned_employees").all())
    source = getattr(ticket, "source_object", None)
    if source is None or not hasattr(source, "items"):
        return items

    current_config_item_ids = set(source.items.values_list("id", flat=True))
    if not current_config_item_ids:
        return []

    filtered = []
    for item in items:
        cfg_item_id = (item.data or {}).get("configuration_item_id")
        try:
            cfg_item_id = int(cfg_item_id)
        except (TypeError, ValueError):
            cfg_item_id = None
        if cfg_item_id in current_config_item_ids:
            filtered.append(item)
    return filtered


def serialize_stages(ticket, items=None):
    """Aggregate planning stages across ProductionItems for a ticket."""
    if items is None:
        items = list(ticket.items.prefetch_related("assigned_employees").all())

    product = getattr(ticket.source_object, "product", None)
    stage_order = list(getattr(product, "production_stages", []) or [])

    stage_data = {}
    for item in items:
        for stage_name, info in (item.planning or {}).items():
            if stage_name not in stage_data:
                stage_data[stage_name] = {"statuses": [], "assigned_employees": {}}
            status = (info or {}).get("status") or "pending"
            stage_data[stage_name]["statuses"].append(status)
            for emp in item.assigned_employees.all():
                stage_data[stage_name]["assigned_employees"][emp.id] = emp.full_name

    def _agg_status(statuses):
        if all(s == "done" for s in statuses):
            return "done"
        if any(s == "in_progress" for s in statuses):
            return "in_progress"
        return "pending"

    def _sort_key(name):
        try:
            return stage_order.index(name)
        except ValueError:
            return 1000

    return [
        {
            "name": name,
            "status": _agg_status(stage_data[name]["statuses"]),
            "assigned_employees": [
                {"id": emp_id, "full_name": emp_name}
                for emp_id, emp_name in stage_data[name]["assigned_employees"].items()
            ],
        }
        for name in sorted(stage_data.keys(), key=_sort_key)
    ]
