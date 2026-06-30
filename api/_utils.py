"""Shared utility helpers for the API views."""

from datetime import date, datetime

from django.http import JsonResponse
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from company.utils import get_current_employee


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


class CompanyAPIView(APIView):
    """Resolves ``request.user``'s company after JWT/session authentication."""

    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        employee = get_current_employee(request.user)
        if employee is None:
            raise PermissionDenied(detail={"error": "User has no employee profile linked."})
        request.employee = employee
        company = employee.company
        if company is None:
            raise PermissionDenied(detail={"error": "Employee profile has no company linked."})
        self.company = company
        self.employee = employee


class SharedTerminalAPIView(APIView):
    """
    For shared-device terminals (e.g. workshop kiosk screens).

    Authenticated by a CompanyUser with can_use_shared_terminal=True.
    Does not require an Employee profile — the terminal account is not a worker.
    Individual employee actions are authorised via their 4-digit work_pin.
    """

    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        from company.models import CompanyUser
        link = (
            CompanyUser.objects.filter(
                user=request.user,
                is_active=True,
                can_use_shared_terminal=True,
            )
            .select_related("company")
            .first()
        )
        if link is None:
            raise PermissionDenied(
                detail={"error": "Esta cuenta no tiene permiso de terminal compartida."}
            )
        self.company = link.company
