from django.http import JsonResponse

from api._utils import CompanyAPIView
from company.models import Employee


class EligibleEmployeesView(CompanyAPIView):
    def get(self, request):
        stage = request.GET.get("stage", "").strip()

        qs = Employee.objects.filter(
            company=self.company,
            can_work_on_production=True,
            is_active=True,
        )
        if stage:
            qs = qs.filter(production_stages__contains=[stage])

        return JsonResponse({
            "employees": [
                {"id": e.id, "full_name": e.full_name}
                for e in qs.order_by("last_name", "first_name")
            ]
        })
