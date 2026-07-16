"""Terminal actor permission checks (Django auth permissions on Employee.user)."""

from rest_framework.exceptions import PermissionDenied

from company.models import Employee

REORDER_ITEMS_PERM = "production.can_reorder_items"


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
        raise PermissionDenied(detail={"error": "Empleado no válido."})

    if not employee_has_perm(employee, perm):
        raise PermissionDenied(detail={"error": "No tienes permiso para esta acción."})

    return employee
