from django.db.models import Q

from core.admin.company_access import get_user_companies


class CompanyRestrictedQuerysetMixin:
    """
    Restricts queryset to objects belonging to the user's companies.
    Assumes the model has a FK called `company` by default.

    Everyone (superusers included) is scoped to the active company set by
    ActiveCompanyMiddleware. Superusers additionally see rows without a
    company so legacy NULL-company records stay reachable.
    """

    company_field_name = "company"  # override per view if needed

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        condition = Q(
            **{f"{self.company_field_name}__in": get_user_companies(user)}
        )
        if user.is_superuser:
            condition |= Q(**{f"{self.company_field_name}__isnull": True})

        return qs.filter(condition)
