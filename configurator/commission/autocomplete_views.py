import json
from django.http import JsonResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.views import View
from django.db.models import Q
from company.models import Employee
from configurator.models import ProductTemplate


@method_decorator(staff_member_required, name="dispatch")
class EmployeeAutocompleteView(View):
    def get(self, request, *args, **kwargs):
        term = request.GET.get("term", "").strip()
        qs = Employee.objects.filter(is_active=True).order_by("last_name", "first_name")
        if term:
            qs = qs.filter(
                Q(first_name__icontains=term) |
                Q(last_name__icontains=term) |
                Q(email__icontains=term)
            )
        # Filter by company if possible
        from company.models import CompanyUser
        company_user = CompanyUser.objects.filter(
            user=request.user
        ).select_related("company").first()
        if company_user:
            qs = qs.filter(company=company_user.company)

        results = [
            {"id": e.pk, "text": f"{e.last_name}, {e.first_name}"}
            for e in qs[:50]
        ]
        return JsonResponse({"results": results, "pagination": {"more": False}})


@method_decorator(staff_member_required, name="dispatch")
class ProductTemplateAutocompleteView(View):
    def get(self, request, *args, **kwargs):
        term = request.GET.get("term", "").strip()
        qs = ProductTemplate.objects.all().order_by("name")
        if term:
            qs = qs.filter(name__icontains=term)
        # Filter by company if possible
        from company.models import CompanyUser
        company_user = CompanyUser.objects.filter(
            user=request.user
        ).select_related("company").first()
        if company_user:
            qs = qs.filter(company=company_user.company)

        results = [
            {"id": pt.pk, "text": pt.name}
            for pt in qs[:50]
        ]
        return JsonResponse({"results": results, "pagination": {"more": False}})