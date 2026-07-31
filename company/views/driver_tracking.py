import json

from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.forms import DriverTrackingFilterForm
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

        filter_form = DriverTrackingFilterForm(self.request.GET or None, drivers=drivers)

        logs = LocationRecord.objects.select_related("employee")
        employee = None
        start_dt = None
        end_dt = None

        if filter_form.is_valid():
            employee = filter_form.cleaned_data.get("employee")
            start_dt = filter_form.cleaned_data.get("start")
            end_dt = filter_form.cleaned_data.get("end")

            if employee:
                logs = logs.filter(employee=employee)
            if start_dt:
                logs = logs.filter(recorded_at__gte=start_dt)
            if end_dt:
                logs = logs.filter(recorded_at__lte=end_dt)

        logs = logs.order_by("recorded_at")

        ctx["filter_form"] = filter_form
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
        ctx["selected_employee"] = employee.pk if employee else None
        ctx["selected_start"] = start_dt
        ctx["selected_end"] = end_dt
        return ctx