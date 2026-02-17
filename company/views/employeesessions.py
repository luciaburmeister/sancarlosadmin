from django.views.generic import ListView
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from company.filters.employeesession_filterform import EmployeeSessionsFilterForm
from production.models import ProductionItemSession, ProductionItemSessionStatus


class EmployeeSessionsView(UnfoldModelAdminViewMixin, ListView):
    title = "Employee sessions"
    permission_required = ("company.view_employee",)
    template_name = "admin/company/employee/sessions.html"

    model = ProductionItemSession  
    paginate_by = 25    

    def get_queryset(self):
        employee = get_object_or_404(Employee, pk=self.kwargs["employee_id"])
        self.employee = employee

        qs = ProductionItemSession.objects.select_related(
            "production_item",
            "production_item__ticket",
            "created_by",
            "completed_by",
        ).filter(created_by=employee)

        form = EmployeeSessionsFilterForm(self.request.GET or None)
        self.filter_form = form

        if form.is_valid():
            from_date = form.cleaned_data.get("from_date")
            to_date = form.cleaned_data.get("to_date")

            if from_date:
                qs = qs.filter(start_time__date__gte=from_date)
            if to_date:
                qs = qs.filter(start_time__date__lte=to_date)

        return qs.order_by("-start_time")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        employee = self.employee
        ctx["employee"] = employee
        ctx["employee_admin_url"] = reverse("admin:company_employee_change", args=[employee.pk])
        ctx["filter_form"] = getattr(self, "filter_form", EmployeeSessionsFilterForm(self.request.GET or None))

        STATUS_COLORS = {
            "pending": "#9ca3af",
            "in_progress": "#f59e0b",
            "done": "#10b981",
            "cancelled": "#ef4444",
        }

        def status_display(status_val: str):
            color = STATUS_COLORS.get(status_val or "", "#60a5fa")

            label = (
                ProductionItemSessionStatus(status_val).label.lower() if status_val else "-")

            return format_html(
                '<span class="inline-flex items-center px-3 py-1 rounded-full text-xs font-medium" '
                'style="background-color: {}; color: #ffffff;">{}</span>',
                color,
                label,
            )

        rows = []
        for s in ctx["object_list"]:  
            item_link = "-"
            ticket_link = "-"

            if s.production_item_id:
                item_link = format_html(
                    '<a class="text-indigo-600 dark:text-indigo-300 hover:underline" href="{}">{}</a>',
                    reverse("admin:production_productionitem_change", args=[s.production_item_id]),
                    s.production_item_id,
                )

            if getattr(s.production_item, "ticket_id", None):
                ticket_link = format_html(
                    '<a class="text-indigo-600 dark:text-indigo-300 hover:underline" href="{}">{}</a>',
                    reverse("admin:production_productionticket_change", args=[s.production_item.ticket_id]),
                    s.production_item.ticket_id,
                )

            rows.append([
                s.start_time.strftime("%b %d, %Y, %H:%M") if s.start_time else "—",
                s.end_time.strftime("%b %d, %Y, %H:%M") if s.end_time else "—",
                str(s.duration) if s.duration else "—",
                s.production_stage or "—",
                mark_safe(status_display(s.status)),
                mark_safe(item_link),
                mark_safe(ticket_link),
            ])

        ctx["table_data"] = {
            "headers": ["Start", "End", "Duration", "Stage", "Status", "Item", "Ticket"],
            "rows": rows,
        }

        return ctx