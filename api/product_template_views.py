"""Endpoints for product templates exposed to mobile configurators."""

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from configurator.models import ProductTemplate

from ._utils import company_or_error


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def product_template_list_view(request):
    company, error = company_or_error(request)
    if error:
        return error
    templates = ProductTemplate.objects.filter(company=company, show_in_configurator=True)
    data = []
    for t in templates:
        params = list(t.parameters.order_by("order").values(
            "id", "name", "label", "type", "options", "order"
        ))
        data.append({"id": t.id, "name": t.name, "parameters": params})
    return JsonResponse({"templates": data})
