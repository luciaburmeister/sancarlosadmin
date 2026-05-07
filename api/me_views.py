"""Endpoints related to the authenticated user / employee profile."""

from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me_view(request):
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
