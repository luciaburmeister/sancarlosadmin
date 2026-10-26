from django.db.models import Q

from unfold.views import BaseAutocompleteView

from company.models import Employee
from core.admin.company_access import get_user_companies


class EmployeeAutocompleteView(BaseAutocompleteView):
    model = Employee

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .filter(is_active=True, company__in=get_user_companies(self.request.user))
            .only("id", "first_name", "last_name")
        )
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term))
        return qs.order_by("last_name", "first_name")
