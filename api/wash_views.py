import json
from datetime import datetime, date
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from djmoney.money import Money

from management.models import ProjectFile
from configurator.models import (
    Configuration,
    ConfigurationStatus,
    ConfigurationFlow,
    ConfigurationParameterValue,
    ProductTemplate,
)
from django.contrib.contenttypes.models import ContentType
from production.models import ProductionTicket
from production.services.stages_to_sessions import StagesToSessionsService
from sales.models import Quote, Client


def _company_or_error(request):
    try:
        employee_profile = request.user.employee_profile
    except Exception:
        return None, JsonResponse({"error": "User has no employee profile linked."}, status=403)
    company = getattr(employee_profile, "company", None)
    if company is None:
        return None, JsonResponse({"error": "Employee profile has no company linked."}, status=403)
    return company, None


def _parse_date_or_error(value):
    if value in (None, ""):
        return None, None
    if isinstance(value, date):
        return value, None
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date(), None
        except ValueError:
            return None, JsonResponse({"error": "Invalid date format. Use YYYY-MM-DD."}, status=400)
    return None, JsonResponse({"error": "Invalid date value."}, status=400)


def _config_total(pf):
    total = 0.0
    for q in pf.quotes.all():
        for c in q.configurations.all():
            if c.price_override:
                total += float(c.price_override.amount)
            elif c.total_price:
                total += float(c.total_price.amount)
    return round(total, 2)


def _serialize_expediente(pf, include_total=False):
    start_date = pf.start_date
    data = {
        "id": pf.id,
        "reference": pf.reference,
        "name": pf.name,
        "client_id": pf.client_id,
        "client": pf.client.name if pf.client else None,
        "status": pf.status,
        "start_date": start_date.strftime("%Y-%m-%d") if hasattr(start_date, "strftime") else start_date,
        "created_at": pf.created_at.strftime("%Y-%m-%d %H:%M"),
    }
    if include_total:
        data["total_price"] = _config_total(pf)
        configs = [c for q in pf.quotes.all() for c in q.configurations.all()]
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


def _serialize_configuration(config):
    return {
        "id": config.id,
        "name": config.name,
        "status": config.status,
        "template_id": config.product_id,
        "template_name": config.product.name if config.product else None,
        "price_override": float(config.price_override.amount) if config.price_override else None,
        "total_price": float(config.total_price.amount) if config.total_price else None,
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


def _serialize_client(c):
    return {"id": c.id, "name": c.name, "phone": c.phone or "", "email": c.email or ""}


# ─── Clients ──────────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wash_me_view(request):
    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    first_name = (employee.first_name or "").strip()
    last_name = (employee.last_name or "").strip()
    full_name = " ".join([p for p in [first_name, last_name] if p]).strip()

    return JsonResponse({
        "employee_first_name": first_name or None,
        "employee_last_name": last_name or None,
        "employee_full_name": full_name or None,
        "username": request.user.username,
    })


# ─── Clients ──────────────────────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def wash_clients_view(request):
    company, error = _company_or_error(request)
    if error:
        return error

    if request.method == "GET":
        search = request.GET.get("search", "").strip()
        clients = Client.objects.filter(company=company).order_by("name")
        if search:
            clients = clients.filter(name__icontains=search)
        return JsonResponse({"clients": [_serialize_client(c) for c in clients]})

    body = json.loads(request.body)
    name = body.get("name", "").strip()
    if not name:
        return JsonResponse({"error": "Name is required"}, status=400)

    client = Client.objects.create(
        company=company,
        name=name,
        phone=body.get("phone", ""),
        email=body.get("email", ""),
    )
    return JsonResponse(_serialize_client(client), status=201)


# ─── Templates ────────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wash_templates_view(request):
    company, error = _company_or_error(request)
    if error:
        return error
    templates = ProductTemplate.objects.filter(company=company, show_in_configurator=True)
    data = []
    for t in templates:
        params = list(t.parameters.order_by("order").values("id", "name", "label", "type", "options", "order"))
        data.append({"id": t.id, "name": t.name, "parameters": params})
    return JsonResponse({"templates": data})


# ─── Expedientes list / create ────────────────────────────────────────────────

@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def wash_expedientes_view(request):
    company, error = _company_or_error(request)
    if error:
        return error

    if request.method == "GET":
        status_filter = request.GET.get("status", "open")   # open | completed | all
        search = request.GET.get("search", "").strip()
        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1

        _PAGE_SIZE = 20

        base_qs = ProjectFile.objects.filter(company=company)
        if search:
            base_qs = base_qs.filter(
                Q(name__icontains=search) | Q(client__name__icontains=search)
            )

        # Counts for both tabs (reflects active search)
        open_count = base_qs.exclude(status__in=["completed", "cancelled"]).count()
        completed_count = base_qs.filter(status="completed").count()

        if status_filter == "open":
            filtered_qs = base_qs.exclude(status__in=["completed", "cancelled"])
        elif status_filter == "completed":
            filtered_qs = base_qs.filter(status="completed")
        else:
            filtered_qs = base_qs

        filtered_qs = filtered_qs.prefetch_related(
            "quotes__configurations__param_values__parameter"
        ).order_by("-created_at")

        paginator = Paginator(filtered_qs, _PAGE_SIZE)
        page = paginator.get_page(page_num)

        return JsonResponse({
            "results": [_serialize_expediente(pf, include_total=True) for pf in page],
            "count": paginator.count,
            "open_count": open_count,
            "completed_count": completed_count,
            "page": page_num,
            "has_next": page.has_next(),
        })

    body = json.loads(request.body)
    client_id = body.get("client_id")
    name = body.get("name", "").strip()

    try:
        client = Client.objects.get(id=client_id, company=company)
    except Client.DoesNotExist:
        return JsonResponse({"error": "Client not found"}, status=404)

    start_date, err = _parse_date_or_error(body.get("date"))
    if err:
        return err
    if not name:
        _months_es = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                      'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre']
        if start_date:
            name = f"{client.name} - {_months_es[start_date.month - 1]} {start_date.year}"
        else:
            name = client.name

    with transaction.atomic():
        project_file = ProjectFile.objects.create(
            company=company, client=client, name=name, start_date=start_date,
        )
        quote = Quote.objects.create(
            company=company, client=client, project_name=name, project_file=project_file,
        )

    data = _serialize_expediente(project_file)
    data["quote_id"] = quote.id
    return JsonResponse(data, status=201)


# ─── Expediente detail / update / delete ──────────────────────────────────────

@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def wash_expediente_detail_view(request, expediente_id):
    company, error = _company_or_error(request)
    if error:
        return error

    try:
        pf = ProjectFile.objects.prefetch_related(
            "quotes__configurations__param_values__parameter",
            "quotes__configurations__product",
        ).get(id=expediente_id, company=company)
    except ProjectFile.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if request.method == "DELETE":
        pf.delete()
        return JsonResponse({"success": True})

    if request.method == "PATCH":
        body = json.loads(request.body)
        if "status" in body:
            pf.status = body["status"]
            pf.save(update_fields=["status"])
        return JsonResponse(_serialize_expediente(pf))

    data = _serialize_expediente(pf)
    data["configurations"] = [
        _serialize_configuration(config)
        for quote in pf.quotes.all()
        for config in quote.configurations.all()
    ]
    return JsonResponse(data)


# ─── Configuration create ─────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wash_expediente_configure_view(request, expediente_id):
    company, error = _company_or_error(request)
    if error:
        return error

    try:
        pf = ProjectFile.objects.get(id=expediente_id, company=company)
    except ProjectFile.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    quote = pf.quotes.first()
    if not quote:
        return JsonResponse({"error": "No quote linked to this expediente"}, status=400)

    body = json.loads(request.body)
    template_id = body.get("template_id")
    param_values = body.get("param_values", {})
    price = body.get("price")

    try:
        template = ProductTemplate.objects.get(id=template_id, company=company)
    except ProductTemplate.DoesNotExist:
        return JsonResponse({"error": "Template not found"}, status=404)

    # Build a meaningful config name from vehicle params
    from datetime import date as _today
    modelo = str(param_values.get('modelo', '')).strip()
    matricula = str(param_values.get('matricula', '')).strip()
    day_str = _today.today().strftime('%d/%m')
    if modelo and matricula:
        config_name = f"{modelo} ({matricula}) - {day_str}"
    elif modelo:
        config_name = f"{modelo} - {day_str}"
    elif matricula:
        config_name = f"{matricula} - {day_str}"
    else:
        config_name = f"{template.name} - {day_str}"

    with transaction.atomic():
        config = Configuration.objects.create(
            company=company,
            client=quote.client,
            quote=quote,
            product=template,
            name=config_name,
            status=ConfigurationStatus.DRAFT,
            price_override=Money(price, "EUR") if price is not None else None,
        )
        params_by_name = {p.name: p for p in template.parameters.all()}
        ConfigurationParameterValue.objects.bulk_create([
            ConfigurationParameterValue(
                configuration=config,
                parameter=param,
                value=str(param.normalize_value(value)),
                order=param.order,
            )
            for param_name, value in param_values.items()
            if (param := params_by_name.get(param_name))
        ])
        config.generate_bom()

    return JsonResponse(_serialize_configuration(config), status=201)


# ─── Configuration edit / delete ──────────────────────────────────────────────

@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def wash_configuration_manage_view(request, expediente_id, config_id):
    company, error = _company_or_error(request)
    if error:
        return error

    try:
        config = Configuration.objects.select_related("product").get(
            id=config_id,
            quote__project_file__id=expediente_id,
            company=company,
        )
    except Configuration.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    if request.method == "DELETE":
        config.delete()
        return JsonResponse({"success": True})

    body = json.loads(request.body)
    param_values = body.get("param_values", {})

    with transaction.atomic():
        update_fields = []

        if "price" in body:
            price = body["price"]
            config.price_override = Money(price, "EUR") if price is not None else None
            update_fields += ["price_override", "price_override_currency"]

        if "status" in body:
            new_status = body["status"]
            if new_status in {s.value for s in ConfigurationStatus}:
                config.status = new_status
                update_fields.append("status")

        if update_fields:
            config.save(update_fields=update_fields)

        if param_values:
            config.param_values.all().delete()
            params_by_name = {p.name: p for p in config.product.parameters.all()}
            ConfigurationParameterValue.objects.bulk_create([
                ConfigurationParameterValue(
                    configuration=config,
                    parameter=param,
                    value=str(param.normalize_value(value)),
                    order=param.order,
                )
                for param_name, value in param_values.items()
                if (param := params_by_name.get(param_name))
            ])
            config.generate_bom()

    return JsonResponse(_serialize_configuration(config))


# ─── Production stages helpers ────────────────────────────────────────────────

def _get_ticket_for_config(config_id, company):
    """Returns (config, ticket, error_response). ticket may be None if not yet converted."""
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


def _serialize_stages(ticket):
    """Aggregate planning stages across all ProductionItems for a ticket."""
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


# ─── Convert configuration to production ──────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wash_configuration_convert_view(request, config_id):
    company, error = _company_or_error(request)
    if error:
        return error

    config, ticket, err = _get_ticket_for_config(config_id, company)
    if err:
        return err

    # If already properly converted and ticket exists, just return current stages
    if ticket is not None and config.status == ConfigurationStatus.CONVERTED:
        return JsonResponse({
            "status": config.status,
            "stages": _serialize_stages(ticket),
        })

    if config.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.COMPLETED]:
        return JsonResponse({"error": "Configuration cannot be converted in its current state."}, status=400)

    try:
        config.generate_bom()
        flow = ConfigurationFlow(config)
        ticket = flow.convert()
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related("items__assigned_employees").get(pk=ticket.pk)

    return JsonResponse({
        "status": config.status,
        "stages": _serialize_stages(ticket),
    })


# ─── Get production stages for a configuration ────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wash_configuration_stages_view(request, config_id):
    company, error = _company_or_error(request)
    if error:
        return error

    config, ticket, err = _get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket found. Convert the configuration first."}, status=404)

    return JsonResponse({
        "ticket_status": ticket.status,
        "stages": _serialize_stages(ticket),
    })


# ─── Eligible employees for a stage ──────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wash_eligible_employees_view(request):
    company, error = _company_or_error(request)
    if error:
        return error

    from company.models import Employee

    stage = request.GET.get("stage", "").strip()

    qs = Employee.objects.filter(company=company, can_work_on_production=True, is_active=True)
    if stage:
        qs = qs.filter(production_stages__contains=[stage])

    return JsonResponse({
        "employees": [
            {"id": e.id, "full_name": e.full_name}
            for e in qs.order_by("last_name", "first_name")
        ]
    })


# ─── Assign employees to a stage ─────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wash_stage_assign_view(request, config_id, stage_name):
    company, error = _company_or_error(request)
    if error:
        return error

    config, ticket, err = _get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    from company.models import Employee

    body = json.loads(request.body)
    employee_ids = body.get("employee_ids", [])
    employees = list(Employee.objects.filter(id__in=employee_ids, company=company))

    for item in ticket.items.prefetch_related("assigned_employees").all():
        if stage_name in (item.planning or {}):
            item.assigned_employees.set(employees)

    ticket = ProductionTicket.objects.prefetch_related("items__assigned_employees").get(pk=ticket.pk)

    return JsonResponse({"success": True, "stages": _serialize_stages(ticket)})


# ─── Start a production stage ─────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wash_stage_start_view(request, config_id, stage_name):
    company, error = _company_or_error(request)
    if error:
        return error

    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    config, ticket, err = _get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    items = list(ticket.items.all())

    try:
        count = StagesToSessionsService.start_activity(
            stage_name=stage_name,
            items=items,
            employee=employee,
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related("items__assigned_employees").get(pk=ticket.pk)

    return JsonResponse({"started": count, "stages": _serialize_stages(ticket)})


# ─── Finish a production stage ────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wash_stage_finish_view(request, config_id, stage_name):
    company, error = _company_or_error(request)
    if error:
        return error

    try:
        employee = request.user.employee_profile
    except Exception:
        return JsonResponse({"error": "User has no employee profile linked."}, status=403)

    config, ticket, err = _get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is None:
        return JsonResponse({"error": "No production ticket. Convert first."}, status=404)

    items = list(ticket.items.all())

    try:
        count = StagesToSessionsService.finish_activity(
            stage_name=stage_name,
            items=items,
            employee=employee,
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related("items__assigned_employees").get(pk=ticket.pk)

    return JsonResponse({"finished": count, "stages": _serialize_stages(ticket)})
