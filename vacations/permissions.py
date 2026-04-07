def has_vacations_privileged_access(user):
    if not user or not user.is_authenticated:
        return False
    return (
        user.is_superuser
        or user.has_perm("vacations.can_view_all_vacations")
        or user.has_perm("vacations.can_approve_vacations")
    )