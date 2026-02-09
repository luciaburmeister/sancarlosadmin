# core/admin/company_access.py

from django.core.cache import cache
from django.db.models import QuerySet
from company.models import Company, CompanyUser

CACHE_TIMEOUT = 60 * 5  # 5 minutes


def get_user_companies(user) -> QuerySet[Company]:
    """
    Returns a queryset of companies the user has access to.
    Superusers get all companies.
    Cached per-user.
    """
    if user.is_superuser:
        return Company.objects.all()

    cache_key = f"admin:user_companies:{user.pk}"
    company_ids = cache.get(cache_key)

    if company_ids is None:
        company_ids = list(
            CompanyUser.objects.filter(
                user=user,
                is_active=True,
                company__is_active=True,
            ).values_list("company_id", flat=True)
        )
        cache.set(cache_key, company_ids, CACHE_TIMEOUT)

    return Company.objects.filter(id__in=company_ids)
