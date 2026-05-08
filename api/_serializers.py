"""Shared serializers and lookup helpers for the API views."""

from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse

from configurator.models import Configuration
from production.models import ProductionTicket


# ─── Project files (a.k.a. expedientes) ───────────────────────────────────────


def config_total(project_file):
    total = 0.0
    for q in project_file.quotes.all():
        for c in q.configurations.all():
            if c.price_override:
                total += float(c.price_override.amount)
            elif c.total_price:
                total += float(c.total_price.amount)
    return round(total, 2)


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
        data["total_price"] = config_total(project_file)
        configs = [c for q in project_file.quotes.all() for c in q.configurations.all()]
        data["configuration_count"] = len(configs)
        services = []
        seen = set()
        for c in configs:
            for pv in c.param_values.all():
                if pv.value in ("true", "True") and pv.parameter.name not in seen:
                    seen.add(pv.parameter.name)
                    services.append(pv.parameter.label or pv.parameter.name)
        data["services"] = services
    return data


# ─── Configurations ───────────────────────────────────────────────────────────


def serialize_configuration(config):
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
        "created_at": config.created_at.strftime("%Y-%m-%d") if config.created_at else None,
    }


# ─── Clients ──────────────────────────────────────────────────────────────────


def serialize_client(c):
    return {"id": c.id, "name": c.name, "phone": c.phone or "", "email": c.email or ""}


# ─── Production tickets and stages ────────────────────────────────────────────


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
