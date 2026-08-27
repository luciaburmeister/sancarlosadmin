"""GET /es/api/employees/ — active employees, for the Laborer Movement picker."""
from django.http import JsonResponse

from api._utils import CompanyAPIView
from company.models import Employee


class EmployeesListView(CompanyAPIView):
    def get(self, request):
        employees = Employee.objects.filter(
            company=self.company, is_active=True,
        ).order_by("first_name", "last_name")
        return JsonResponse({
            "results": [
                {"id": e.id, "name": f"{e.first_name} {e.last_name}".strip()}
                for e in employees
            ],
        })
