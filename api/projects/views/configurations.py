"""CRUD + preview + convert endpoints for Configuration."""

import json

from django.db import transaction
from django.http import JsonResponse
from djmoney.money import Money

from api._serializers import (
    active_ticket_items,
    get_ticket_for_config,
    serialize_configuration,
    serialize_product_config,
    serialize_production_items_with_stages,
)
from api._utils import CompanyAPIView
from api.projects.helpers import default_config_name, missing_required_params
from configurator.models import (
    Configuration,
    ConfigurationFlow,
    ConfigurationParameterValue,
    ConfigurationStatus,
    ProductTemplate,
)
from management.models import ProjectFile
from production.models import ProductionTicket


class ConfigurationPreviewView(CompanyAPIView):
    def post(self, request):
        body = json.loads(request.body)
        template_id = body.get("template_id")
        param_values = body.get("param_values", {})
        price = body.get("price")

        try:
            template = ProductTemplate.objects.get(id=template_id, company=self.company)
        except ProductTemplate.DoesNotExist:
            return JsonResponse({"error": "Template not found"}, status=404)

        try:
            with transaction.atomic():
                config = Configuration.objects.create(
                    company=self.company,
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
                data = serialize_product_config(config)
                transaction.set_rollback(True)
        except Exception as exc:
            return JsonResponse(
                {"error": "preview_failed", "detail": str(exc)},
                status=400,
            )

        return JsonResponse(data)


class ConfigurationCreateView(CompanyAPIView):
    def post(self, request, project_file_id):
        try:
            pf = ProjectFile.objects.get(id=project_file_id, company=self.company)
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
            template = ProductTemplate.objects.get(id=template_id, company=self.company)
        except ProductTemplate.DoesNotExist:
            return JsonResponse({"error": "Template not found"}, status=404)

        missing_required = missing_required_params(template, param_values)
        if missing_required:
            return JsonResponse(
                {"error": "Missing required parameters", "fields": missing_required},
                status=400,
            )

        if not config_name:
            config_name = default_config_name(template)

        with transaction.atomic():
            config = Configuration.objects.create(
                company=self.company,
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


class ConfigurationManageView(CompanyAPIView):
    def patch(self, request, project_file_id, config_id):
        try:
            config = Configuration.objects.select_related("product").get(
                id=config_id,
                quote__project_file__id=project_file_id,
                company=self.company,
            )
        except Configuration.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        param_values = body.get("param_values", {})

        if "param_values" in body:
            missing_required = missing_required_params(config.product, param_values)
            if missing_required:
                return JsonResponse(
                    {"error": "Missing required parameters", "fields": missing_required},
                    status=400,
                )

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

    def delete(self, request, project_file_id, config_id):
        try:
            config = Configuration.objects.get(
                id=config_id,
                quote__project_file__id=project_file_id,
                company=self.company,
            )
        except Configuration.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        config.delete()
        return JsonResponse({"success": True})


class ConfigurationConvertView(CompanyAPIView):
    def post(self, request, config_id):
        config, ticket, err = get_ticket_for_config(config_id, self.company)
        if err:
            return err

        if ticket is not None and config.status == ConfigurationStatus.CONVERTED:
            items = active_ticket_items(ticket)
            return JsonResponse({
                "status": config.status,
                "ticket_status": ticket.status,
                "production_items": serialize_production_items_with_stages(ticket, items=items),
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
        items = active_ticket_items(ticket)

        return JsonResponse({
            "status": config.status,
            "ticket_status": ticket.status,
            "production_items": serialize_production_items_with_stages(ticket, items=items),
        })
