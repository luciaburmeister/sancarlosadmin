"""Shared serializers and lookup helpers for the API views."""

import unicodedata

from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse

from configurator.models import Configuration
from production.models import ProductionItemSession, ProductionItemSessionStatus, ProductionTicket


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


def _batch_stage_summaries(config_ids):
    """Return {config_id: {total, completed, in_progress}} in a single DB round-trip."""
    if not config_ids:
        return {}
    from production.models import ProductionItemStatus
    ct = ContentType.objects.get_for_model(Configuration)
    tickets = list(
        ProductionTicket.objects
        .filter(content_type=ct, object_id__in=config_ids)
        .prefetch_related("items")
    )
    ticket_by_config = {t.object_id: t for t in tickets}
    result = {}
    for config_id in config_ids:
        ticket = ticket_by_config.get(config_id)
        total = completed = in_progress = 0
        if ticket:
            for item in ticket.items.all():
                for info in (item.planning or {}).values():
                    status = (info or {}).get("status") or "pending"
                    total += 1
                    if status == ProductionItemStatus.DONE:
                        completed += 1
                    elif status == ProductionItemStatus.IN_PROGRESS:
                        in_progress += 1
        result[config_id] = {"total": total, "completed": completed, "in_progress": in_progress}
    return result


def serialize_product_config(config, stage_summary=None):
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
        "stage_summary": stage_summary or {"total": 0, "completed": 0, "in_progress": 0},
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
        "internal_name": quote.internal_name or quote.project_name,
        "status": quote.status,
        "created_at": quote.created_at.strftime("%Y-%m-%d %H:%M"),
        "expires_at": quote.expires_at.strftime("%Y-%m-%d") if quote.expires_at else None,
        "total_price": float(quote.total_price.amount) if quote.total_price else 0.0,
        "client_name": client_name,
        "observations": quote.observations or "",
    }
    if include_products:
        configs = list(
            quote.configurations
            .select_related("product")
            .prefetch_related("param_values__parameter")
            .all()
        )
        data["product_count"] = len(configs)
        config_ids = [c.id for c in configs]
        stage_summaries = _batch_stage_summaries(config_ids)
        data["products"] = [
            serialize_product_config(c, stage_summary=stage_summaries.get(c.id))
            for c in configs
        ]
        invoiced_count = SalesInvoice.objects.filter(
            configuration_id__in=config_ids
        ).values("configuration_id").distinct().count()
        data["is_invoiced"] = bool(config_ids) and invoiced_count >= len(config_ids)
    else:
        data["product_count"] = quote.configurations.count()
        data["is_invoiced"] = False
    return data


# ─── Configurations ───────────────────────────────────────────────────────────


def serialize_configuration_stages_context(config: Configuration) -> dict:
    """Summary for mobile production-stages (matrícula, modelo, nombre de la configuración)."""
    matricula = ""
    modelo = ""
    for pv in config.param_values.all():
        pname = getattr(pv.parameter, "name", None) or ""
        if pname == "matricula":
            matricula = str(pv.value or "").strip()
        elif pname == "modelo":
            modelo = str(pv.value or "").strip()
    quote = getattr(config, "quote", None)
    quote_name = (quote.project_name or "").strip() if quote else ""
    return {
        "matricula": matricula,
        "modelo": modelo,
        "configuration_name": (config.name or "").strip(),
        "template_name": config.product.name if getattr(config, "product_id", None) else None,
        "quote_name": quote_name or None,
    }


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


# ─── Quote photos ─────────────────────────────────────────────────────────────


def serialize_photo_category(cat):
    return {
        "id": cat.id,
        "name": cat.name,
        "order": cat.order,
        "is_active": cat.is_active,
    }


def serialize_quote_photo(photo, request=None):
    if request:
        image_url = request.build_absolute_uri(photo.image.url)
    else:
        image_url = photo.image.url if photo.image else None
    return {
        "id": photo.id,
        "quote_id": photo.quote_id,
        "category_id": photo.category_id,
        "category_name": photo.category.name if photo.category else None,
        "image_url": image_url,
        "caption": photo.caption,
        "uploaded_at": photo.uploaded_at.strftime("%Y-%m-%d %H:%M"),
    }


# ─── Clients ──────────────────────────────────────────────────────────────────


def serialize_client(c):
    from sales.services.client_terms import client_terms_status

    terms = client_terms_status(client=c, company=c.company)
    return {
        "id": c.id,
        "name": c.name,
        "tax_identifier": c.tax_identifier or "",
        "phone": c.phone or "",
        "email": c.email or "",
        "address": c.address or "",
        "payment_method": c.payment_method or "transfer",
        "terms_accepted_at": c.terms_accepted_at.isoformat() if c.terms_accepted_at else None,
        "has_signature": terms["has_signature"],
        "terms_version": terms["terms_version"],
        "current_terms_version": terms["current_terms_version"],
        "needs_terms_signature": terms["needs_terms_signature"],
        "terms_outdated": terms["terms_outdated"],
    }


# ─── Production tickets and stages ────────────────────────────────────────────


def _config_needs_assignment(config):
    """
    Returns True when a production config has at least one pending/in-progress
    stage without any employee assigned to its item (line-level M2M).
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
        config = (
            Configuration.objects.select_related("product", "quote")
            .prefetch_related("param_values__parameter")
            .get(id=config_id, company=company)
        )
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


def _sort_production_items_stable(items):
    """Stable API order: BOM line (configuration_item_id), then ProductionItem pk.

    Avoids non-deterministic DB row order so mobile lists do not jump after updates.
    """
    def sort_key(item):
        data = item.data or {}
        raw = data.get("configuration_item_id")
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            return (1, item.id)  # lines without BOM id: preserve id order among them
        return (0, cid, item.id)

    return sorted(items, key=sort_key)


def active_ticket_items(ticket):
    """Return ProductionItems that still belong to the current configuration BOM.

    If the ticket source is not a Configuration (or cannot be resolved), return all
    ticket items to preserve backward-compatible behavior.
    """
    items = list(
        ticket.items.prefetch_related("assigned_employees").order_by("id")
    )
    source = getattr(ticket, "source_object", None)
    if source is None or not hasattr(source, "items"):
        return _sort_production_items_stable(items)

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
    return _sort_production_items_stable(filtered)


def _ordered_stage_names(item) -> list[str]:
    order = [
        unicodedata.normalize("NFC", s)
        for s in (getattr(item, "production_stages_order", None) or [])
    ]
    planning = item.planning or {}
    names = {unicodedata.normalize("NFC", k) for k in planning.keys()}

    def sort_key(name: str):
        try:
            return (0, order.index(name))
        except ValueError:
            return (1, name)

    return sorted(names, key=sort_key)


def serialize_production_items_with_stages(ticket, items=None, employee=None) -> list[dict]:
    """One entry per ProductionItem with nested stages (BOM line, not aggregated)."""
    if items is None:
        items = list(
            ticket.items.prefetch_related("assigned_employees").order_by("id")
        )
    items = _sort_production_items_stable(items)

    employee_stages = set()
    if employee is not None:
        employee_stages = {
            unicodedata.normalize("NFC", s)
            for s in (employee.production_stages or [])
        }
    # Empty set means no restrictions (employee can work any stage).
    employee_unrestricted = not employee_stages

    item_ids = [it.id for it in items]

    # Open sessions → active_workers index.
    open_sessions = (
        ProductionItemSession.objects.filter(
            production_item_id__in=item_ids,
            end_time__isnull=True,
            status=ProductionItemSessionStatus.IN_PROGRESS,
        )
        .select_related("created_by")
    )
    active_workers: dict[tuple, list] = {}
    for sess in open_sessions:
        key = (sess.production_item_id, unicodedata.normalize("NFC", sess.production_stage))
        emp = sess.created_by
        if emp:
            active_workers.setdefault(key, []).append(
                {"id": emp.id, "full_name": emp.full_name}
            )

    # Closed sessions → paused_workers index (was here but no longer active).
    closed_sessions = (
        ProductionItemSession.objects.filter(
            production_item_id__in=item_ids,
            end_time__isnull=False,
        )
        .select_related("created_by")
    )
    # (item_id, stage_name) -> {employee_id: employee_dict}
    had_session: dict[tuple, dict] = {}
    for sess in closed_sessions:
        emp = sess.created_by
        if emp is None:
            continue
        key = (sess.production_item_id, unicodedata.normalize("NFC", sess.production_stage))
        had_session.setdefault(key, {})[emp.id] = {"id": emp.id, "full_name": emp.full_name}

    out = []
    for item in items:
        data = item.data or {}
        cfg_item_id = data.get("configuration_item_id")
        try:
            cfg_id_int = int(cfg_item_id) if cfg_item_id is not None else None
        except (TypeError, ValueError):
            cfg_id_int = None

        line_assignees = [
            {"id": e.id, "full_name": e.full_name}
            for e in item.assigned_employees.all()
        ]

        stages_out = []
        for stage_name in _ordered_stage_names(item):
            raw_info = {}
            for k, v in (item.planning or {}).items():
                if unicodedata.normalize("NFC", k) == stage_name:
                    raw_info = v if isinstance(v, dict) else {}
                    break
            status = (raw_info.get("status") or "pending") if isinstance(raw_info, dict) else "pending"
            workers = active_workers.get((item.id, stage_name), [])
            active_ids = {w["id"] for w in workers}

            # Employees who worked this stage previously but are not active now.
            paused_workers = [
                w for eid, w in had_session.get((item.id, stage_name), {}).items()
                if eid not in active_ids
            ]

            employee_already_active = employee is not None and employee.id in active_ids
            can_start_session = (
                employee is not None
                and status != "done"
                and (employee_unrestricted or stage_name in employee_stages)
                and not employee_already_active
            )
            stages_out.append(
                {
                    "name": stage_name,
                    "status": status,
                    "assigned_employees": line_assignees,
                    "active_workers": workers,
                    "paused_workers": paused_workers,
                    "can_start_session": can_start_session,
                }
            )

        out.append(
            {
                "production_item_id": item.id,
                "configuration_item_id": cfg_id_int,
                "component_label": (
                    data.get("component")
                    or (str(item.component) if getattr(item, "component_id", None) else "—")
                ),
                "quantity": float(item.quantity) if item.quantity is not None else None,
                "length": float(item.length) if item.length is not None else None,
                "stages": stages_out,
            }
        )
    return out
