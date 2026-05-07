"""CRUD + preview + convert endpoints for Configuration."""

import json
from datetime import date

from django.db import transaction
from django.http import JsonResponse
from djmoney.money import Money
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from configurator.models import (
    Configuration,
    ConfigurationFlow,
    ConfigurationParameterValue,
    ConfigurationStatus,
    ProductTemplate,
)
from management.models import ProjectFile
from production.models import ProductionTicket

from ._serializers import (
    get_ticket_for_config,
    serialize_configuration,
    serialize_stages,
)
from ._utils import company_or_error


def _default_config_name(template):
    return f"{template.name} - {date.today().strftime('%d/%m')}"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def configuration_preview_view(request):
    """Compute items + subtotal for a template + param_values without persisting."""
    company, error = company_or_error(request)
    if error:
        return error

    body = json.loads(request.body)
    template_id = body.get("template_id")
    param_values = body.get("param_values", {})
    price = body.get("price")

    try:
        template = ProductTemplate.objects.get(id=template_id, company=company)
    except ProductTemplate.DoesNotExist:
        return JsonResponse({"error": "Template not found"}, status=404)

    sid = transaction.savepoint()
    try:
        config = Configuration.objects.create(
            company=company,
            product=template,
            name="__preview__",
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
        config.recalculate_price()
        config.refresh_from_db()
        data = serialize_configuration(config)
    finally:
        transaction.savepoint_rollback(sid)

    return JsonResponse(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def configuration_create_view(request, project_file_id):
    """Create a Configuration inside a ProjectFile.

    Accepts an optional ``name`` in the body. If not provided, a default name
    based on the template name and current date is used.
    """
    company, error = company_or_error(request)
    if error:
        return error

    try:
        pf = ProjectFile.objects.get(id=project_file_id, company=company)
    except ProjectFile.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)

    quote = pf.quotes.first()
    if not quote:
        return JsonResponse({"error": "No quote linked to this project file"}, status=400)

    body = json.loads(request.body)
    template_id = body.get("template_id")
    param_values = body.get("param_values", {})
    price = body.get("price")
    config_name = (body.get("name") or "").strip()

    try:
        template = ProductTemplate.objects.get(id=template_id, company=company)
    except ProductTemplate.DoesNotExist:
        return JsonResponse({"error": "Template not found"}, status=404)

    if not config_name:
        config_name = _default_config_name(template)

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
        config.recalculate_price()

    return JsonResponse(serialize_configuration(config), status=201)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def configuration_manage_view(request, project_file_id, config_id):
    company, error = company_or_error(request)
    if error:
        return error

    try:
        config = Configuration.objects.select_related("product").get(
            id=config_id,
            quote__project_file__id=project_file_id,
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
        price_changed = False

        if "price" in body:
            price = body["price"]
            config.price_override = Money(price, "EUR") if price is not None else None
            update_fields += ["price_override", "price_override_currency"]
            price_changed = True

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
            price_changed = True

        if price_changed:
            config.recalculate_price()

    return JsonResponse(serialize_configuration(config))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def configuration_convert_view(request, config_id):
    """Convert a Configuration into a ProductionTicket."""
    company, error = company_or_error(request)
    if error:
        return error

    config, ticket, err = get_ticket_for_config(config_id, company)
    if err:
        return err

    if ticket is not None and config.status == ConfigurationStatus.CONVERTED:
        return JsonResponse({
            "status": config.status,
            "stages": serialize_stages(ticket),
        })

    if config.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.COMPLETED]:
        return JsonResponse(
            {"error": "Configuration cannot be converted in its current state."},
            status=400,
        )

    try:
        config.generate_bom()
        flow = ConfigurationFlow(config)
        ticket = flow.convert()
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    ticket = ProductionTicket.objects.prefetch_related(
        "items__assigned_employees"
    ).get(pk=ticket.pk)

    return JsonResponse({
        "status": config.status,
        "stages": serialize_stages(ticket),
    })
