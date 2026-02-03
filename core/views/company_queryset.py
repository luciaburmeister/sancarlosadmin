from core.admin.company_access import get_user_companies


class CompanyRestrictedQuerysetMixin:
    """
    Restricts queryset to objects belonging to the user's companies.
    Assumes the model has a FK called `company` by default.
    """

    company_field_name = "company"  # override per view if needed

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if user.is_superuser:
            return qs

        return qs.filter(
            **{f"{self.company_field_name}__in": get_user_companies(user)}
        )
