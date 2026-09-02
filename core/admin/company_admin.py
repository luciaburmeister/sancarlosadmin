# core/admin/company_admin.py

from django.contrib import admin
from django.db.models import Q

from company.active_company import ActiveCompany
from core.admin.company_access import get_user_companies


class CompanyRestrictedAdmin(admin.ModelAdmin):
    """
    Base admin class for models with a `company` FK.

    Everyone (superusers included) is scoped to the active companies set by
    ActiveCompanyMiddleware (one or more, toggled from the switcher).
    Superusers additionally see rows without a company so legacy
    NULL-company records stay reachable and fixable.

    With a single active company the company column and filter are
    redundant and hidden; with several they come back so rows from
    different companies can be told apart.
    """

    company_field_name = "company"  # override if needed

    @staticmethod
    def _multiple_companies_active() -> bool:
        return len(ActiveCompany.get_current_ids() or []) > 1

    def _company_is_direct_field(self) -> bool:
        if "__" in self.company_field_name:
            return False
        try:
            self.model._meta.get_field(self.company_field_name)
        except Exception:
            return False
        return True

    def get_queryset(self, request):
        qs = super().get_queryset(request)

        condition = Q(
            **{f"{self.company_field_name}__in": get_user_companies(request.user)}
        )
        if request.user.is_superuser:
            condition |= Q(**{f"{self.company_field_name}__isnull": True})

        qs = qs.filter(condition)
        if self._multiple_companies_active() and self._company_is_direct_field():
            # The company column is shown; avoid one query per row.
            qs = qs.select_related(self.company_field_name)
        return qs

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Restrict company choices in admin forms.
        """
        if db_field.name == self.company_field_name:
            kwargs["queryset"] = get_user_companies(request.user)

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_list_display(self, request):
        """
        Append the company column when more than one company is active so
        rows can be told apart. Untouched with a single active company.
        """
        list_display = list(super().get_list_display(request))
        if (
            self._multiple_companies_active()
            and self._company_is_direct_field()
            and self.company_field_name not in list_display
        ):
            list_display.append(self.company_field_name)
        return list_display

    def get_list_filter(self, request):
        """
        Remove the company filter with a single active company (it would
        only ever have one option); keep it when several are active.
        """
        filters = super().get_list_filter(request)
        if self._multiple_companies_active():
            return filters
        return [
            f
            for f in filters
            if f != self.company_field_name
            and not (isinstance(f, (tuple, list)) and f[0] == self.company_field_name)
        ]
