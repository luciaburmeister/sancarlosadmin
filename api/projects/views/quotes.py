"""Endpoints for Presupuesto (Quote) and product configurations within a quote."""

import json
from datetime import date

from django.db import transaction
from django.http import JsonResponse
from djmoney.money import Money

from api._serializers import serialize_presupuesto, serialize_product_config
from api._utils import CompanyAPIView, parse_date_or_error
from api.projects.helpers import default_config_name, missing_required_params
from configurator.models import (
    Configuration,
    ConfigurationFlow,
    ConfigurationParameterValue,
    ConfigurationStatus,
    ProductTemplate,
)
from management.models import ProjectFile, ProjectFileStatus
from sales.models import Quote

_ALLOWED_QUOTE_STATUSES = {"draft", "sent", "accepted", "rejected"}


class PresupuestoListCreateView(CompanyAPIView):
    def get(self, request, project_file_id):
        try:
            pf = ProjectFile.objects.get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        quotes = pf.quotes.prefetch_related("configurations").order_by("-created_at")
        results = [serialize_presupuesto(q, include_products=False) for q in quotes]
        return JsonResponse({"results": results, "count": len(results)})

    def post(self, request, project_file_id):
        try:
            pf = ProjectFile.objects.get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        name = (body.get("name") or "").strip()
        expires_at, err = parse_date_or_error(body.get("expires_at"))
        if err:
            return err

        if not name:
            name = f"Presupuesto {date.today().strftime('%d/%m/%Y')}"

        had_quotes = pf.quotes.exists()
        quote = Quote.objects.create(
            company=self.company,
            client=pf.client,
            project_name=name,
            project_file=pf,
            expires_at=expires_at,
        )
        if not had_quotes and pf.status == ProjectFileStatus.NEW:
            pf.status = ProjectFileStatus.ACTIVE
            pf.save(update_fields=["status", "updated_at"])
        elif pf.status == ProjectFileStatus.COMPLETED:
            pf.status = ProjectFileStatus.ACTIVE
            pf.save(update_fields=["status", "updated_at"])
        return JsonResponse(serialize_presupuesto(quote, include_products=False), status=201)


class PresupuestoDetailView(CompanyAPIView):
    def get(self, request, quote_id):
        try:
            quote = (
                Quote.objects
                .prefetch_related(
                    "configurations__product",
                    "configurations__param_values__parameter",
                )
                .get(id=quote_id, company=self.company)
            )
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        return JsonResponse(serialize_presupuesto(quote))

    def patch(self, request, quote_id):
        try:
            quote = Quote.objects.get(id=quote_id, company=self.company)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        update_fields = []
        if "status" in body:
            new_status = body["status"]
            if new_status not in _ALLOWED_QUOTE_STATUSES:
                return JsonResponse({"error": f"Invalid status: {new_status}"}, status=400)
            quote.status = new_status
            update_fields.append("status")
        if "name" in body:
            quote.project_name = (body["name"] or "").strip()
            update_fields.append("project_name")
        if update_fields:
            quote.save(update_fields=update_fields)

        quote = (
            Quote.objects
            .prefetch_related(
                "configurations__product",
                "configurations__param_values__parameter",
            )
            .get(id=quote_id)
        )
        return JsonResponse(serialize_presupuesto(quote))

    def delete(self, request, quote_id):
        try:
            quote = Quote.objects.get(id=quote_id, company=self.company)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        if quote.configurations.exists():
            return JsonResponse(
                {"error": "No se puede eliminar un presupuesto con productos."},
                status=400,
            )
        quote.delete()
        return JsonResponse({"success": True})


class ProductoListCreateView(CompanyAPIView):
    def post(self, request, quote_id):
        try:
            quote = Quote.objects.select_related("client").get(id=quote_id, company=self.company)
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        template_id = body.get("template_id")
        param_values = body.get("param_values", {})
        price = body.get("price")
        config_name = (body.get("name") or "").strip()

        try:
            template = ProductTemplate.objects.get(id=template_id, company=self.company)
        except ProductTemplate.DoesNotExist:
            return JsonResponse({"error": "Template not found"}, status=404)

        missing = missing_required_params(template, param_values)
        if missing:
            return JsonResponse({"error": "Missing required parameters", "fields": missing}, status=400)

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
            config.copy_default_pricing_rules_from_product()
            config.copy_default_taxes_from_product()
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

            try:
                flow = ConfigurationFlow(config)
                flow.convert()
            except Exception:
                pass

            config.refresh_from_db()
            quote.recalc_totals()

        return JsonResponse(serialize_product_config(config), status=201)


class ProductoDetailView(CompanyAPIView):
    def patch(self, request, quote_id, config_id):
        try:
            config = Configuration.objects.select_related("product", "quote").get(
                id=config_id,
                quote_id=quote_id,
                company=self.company,
            )
        except Configuration.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        param_values = body.get("param_values", {})

        if "param_values" in body:
            missing = missing_required_params(config.product, param_values)
            if missing:
                return JsonResponse(
                    {"error": "Missing required parameters", "fields": missing}, status=400
                )

        with transaction.atomic():
            update_fields = []
            price_changed = False

            if "price" in body:
                price = body["price"]
                config.price_override = Money(price, "EUR") if price is not None else None
                update_fields += ["price_override", "price_override_currency"]
                price_changed = True

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
                config.refresh_from_db()
                if config.quote:
                    config.quote.recalc_totals()

        return JsonResponse(serialize_product_config(config))

    def delete(self, request, quote_id, config_id):
        try:
            config = Configuration.objects.select_related("product", "quote").get(
                id=config_id,
                quote_id=quote_id,
                company=self.company,
            )
        except Configuration.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        quote = config.quote
        config.delete()
        if quote:
            quote.recalc_totals()
        return JsonResponse({"success": True})
