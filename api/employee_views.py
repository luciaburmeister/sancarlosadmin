"""Endpoints to query employees (e.g. eligible for production stages)."""

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from company.models import Employee

from ._utils import company_or_error


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def eligible_employees_view(request):
    company, error = company_or_error(request)
    if error:
        return error

    stage = request.GET.get("stage", "").strip()

    qs = Employee.objects.filter(
        company=company,
        can_work_on_production=True,
        is_active=True,
    )
    if stage:
        qs = qs.filter(production_stages__contains=[stage])

    return JsonResponse({
        "employees": [
            {"id": e.id, "full_name": e.full_name}
            for e in qs.order_by("last_name", "first_name")
        ]
    })
