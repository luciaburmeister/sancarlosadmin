# core/admin/company_admin.py

from django.contrib import admin
from django.db.models import Q

from core.admin.company_access import get_user_companies


class CompanyRestrictedAdmin(admin.ModelAdmin):
    """
    Base admin class for models with a `company` FK.

    Everyone (superusers included) is scoped to the active company set by
    ActiveCompanyMiddleware. Superusers additionally see rows without a
    company so legacy NULL-company records stay reachable and fixable.
    """

    company_field_name = "company"  # override if needed

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        condition = Q(
            **{f"{self.company_field_name}__in": get_user_companies(request.user)}
        )
        if request.user.is_superuser:
            condition |= Q(**{f"{self.company_field_name}__isnull": True})

        return qs.filter(condition)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Restrict company choices in admin forms.
        """
        if db_field.name == self.company_field_name:
            kwargs["queryset"] = get_user_companies(request.user)

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_list_filter(self, request):
        """
        Remove the company filter: with a single active company it would
        only ever have one option.
        """
        filters = super().get_list_filter(request)
        return [f for f in filters if f != self.company_field_name]
