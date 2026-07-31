import json

from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.models import LocationRecord, Employee


class DriverTrackingView(UnfoldModelAdminViewMixin, TemplateView):
    template_name = "admin/company/employee/driver_tracking.html"
    permission_required = ("company.view_locationrecord",)
    title = _("Tracking de conductores")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        drivers = (
            Employee.objects.filter(location_records__isnull=False)
            .distinct()
            .order_by("last_name", "first_name")
        )

        employee_id = self.request.GET.get("employee")
        date_str = self.request.GET.get("date")

        logs = LocationRecord.objects.select_related("employee")

        if employee_id:
            logs = logs.filter(employee_id=employee_id)

        if date_str:
            try:
                from datetime import datetime
                date = datetime.strptime(date_str, "%Y-%m-%d").date()
                logs = logs.filter(recorded_at__date=date)
            except ValueError:
                pass

        logs = logs.order_by("recorded_at")

        ctx["drivers"] = drivers
        ctx["logs"] = logs
        ctx["locations_json"] = json.dumps([
            {
                "lat": float(log.latitude),
                "lon": float(log.longitude),
                "timestamp": log.recorded_at.strftime("%d/%m/%Y %H:%M"),
                "driver": log.employee.full_name,
            }
            for log in logs
        ])
        ctx["selected_employee"] = employee_id
        ctx["selected_date"] = date_str
        return ctx