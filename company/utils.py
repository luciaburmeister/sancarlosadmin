from company.models import Employee


def get_current_employee(user):
    """Return the active Employee linked to this user, if any."""
    if not user or not user.is_authenticated:
        return None
    return (
        Employee.objects.filter(user=user, is_active=True)
        .select_related("company")
        .first()
    )
