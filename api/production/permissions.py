"""Terminal actor permission checks (Django auth permissions on Employee.user)."""

from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import PermissionDenied

from company.models import Employee


def employee_has_perm(employee: Employee, perm: str) -> bool:
    return bool(employee.user_id and employee.user.has_perm(perm))


def get_employee_with_perm(*, company, employee_id: int, perm: str) -> Employee:
    """Return an active employee of *company* that holds *perm* on their User."""
    try:
        employee = Employee.objects.select_related("user").get(
            id=employee_id,
            company=company,
            is_active=True,
        )
    except Employee.DoesNotExist:
        raise PermissionDenied(detail={"error": str(_("Invalid employee."))})

    if not employee_has_perm(employee, perm):
        raise PermissionDenied(detail={"error": str(_("You don't have permission for this action."))})

    return employee
