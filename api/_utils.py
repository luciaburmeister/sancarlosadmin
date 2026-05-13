"""Shared utility helpers for the API views."""

from datetime import date, datetime

from django.http import JsonResponse


def company_or_error(request):
    """Return (company, None) for the authenticated user, or (None, error_response)."""
    try:
        employee_profile = request.user.employee_profile
    except Exception:
        return None, JsonResponse({"error": "User has no employee profile linked."}, status=403)
    company = getattr(employee_profile, "company", None)
    if company is None:
        return None, JsonResponse({"error": "Employee profile has no company linked."}, status=403)
    return company, None


def parse_date_or_error(value):
    """Parse a YYYY-MM-DD date or return (None, error_response)."""
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
