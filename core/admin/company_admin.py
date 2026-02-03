# core/admin/company_admin.py

from django.contrib import admin
from core.admin.company_access import get_user_companies


class CompanyRestrictedAdmin(admin.ModelAdmin):
    """
    Base admin class for models with a `company` FK.
    """

    company_field_name = "company"  # override if needed

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        if request.user.is_superuser:
            return qs

        return qs.filter(
            **{f"{self.company_field_name}__in": get_user_companies(request.user)}
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Restrict company choices in admin forms.
        """
        if db_field.name == self.company_field_name and not request.user.is_superuser:
            kwargs["queryset"] = get_user_companies(request.user)

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_list_filter(self, request):
        """
        Optional: remove company filter for non-superusers
        to avoid leaking other companies.
        """
        filters = super().get_list_filter(request)
        if request.user.is_superuser:
            return filters
        return [f for f in filters if f != self.company_field_name]
