import json
from datetime import datetime, date
from django.http import JsonResponse
from django.db import transaction
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from djmoney.money import Money

from management.models import ProjectFile
from configurator.models import (
    Configuration,
    ConfigurationStatus,
    ConfigurationParameterValue,
    ProductTemplate,
)
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
    }


def _serialize_client(c):
    return {"id": c.id, "name": c.name, "phone": c.phone or "", "email": c.email or ""}


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
        date_str = request.GET.get("date")
        qs = ProjectFile.objects.filter(company=company).prefetch_related(
            "quotes__configurations__param_values__parameter"
        ).order_by("-created_at")
        if date_str:
            qs = qs.filter(start_date=date_str)
        return JsonResponse({"expedientes": [_serialize_expediente(pf, include_total=True) for pf in qs]})

    body = json.loads(request.body)
    client_id = body.get("client_id")
    vehicle = body.get("vehicle", "")
    plate = body.get("plate", "")

    try:
        client = Client.objects.get(id=client_id, company=company)
    except Client.DoesNotExist:
        return JsonResponse({"error": "Client not found"}, status=404)

    parts = [p for p in [vehicle, plate] if p]
    name = " - ".join(parts) if parts else "Vehicle"
    start_date, err = _parse_date_or_error(body.get("date"))
    if err:
        return err

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

    with transaction.atomic():
        config = Configuration.objects.create(
            company=company,
            client=quote.client,
            quote=quote,
            product=template,
            name=pf.name,
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
    price = body.get("price")
    param_values = body.get("param_values", {})

    with transaction.atomic():
        config.price_override = Money(price, "EUR") if price is not None else None
        config.save(update_fields=["price_override", "price_override_currency"])

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

    return JsonResponse(_serialize_configuration(config))
