# core/admin/company_access.py

from django.db.models import QuerySet

from company.active_company import ActiveCompany
from company.models import Company

# Kept for backwards compatibility with tests/utilities that import it.
CACHE_TIMEOUT = ActiveCompany.CACHE_TIMEOUT


def get_user_allowed_companies(user) -> QuerySet[Company]:
    """
    Every company the user has access to, ignoring the active-company
    selection. Superusers get all companies. Cached per-user.
    """
    return ActiveCompany.allowed_for(user)


def get_user_companies(user) -> QuerySet[Company]:
    """
    Companies visible to the user RIGHT NOW.

    Inside a request, ActiveCompanyMiddleware has set the active companies
    (one or more, toggled from the switcher), so this returns just those.
    Outside requests (celery, shell, management commands) the holder is
    unset and the full allowed list is returned, matching the pre-switcher
    behavior.
    """
    allowed = ActiveCompany.allowed_for(user)
    active_ids = ActiveCompany.get_current_ids()

    if active_ids is not None:
        # Intersection: the narrowing can only shrink the allowed set,
        # never widen it, even if a different user object is passed.
        return allowed.filter(pk__in=active_ids)

    return allowed
