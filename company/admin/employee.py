from django.contrib import admin
from unfold.admin import ModelAdmin

from django.urls import path, reverse
from company.models import Employee
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm, SelectableFieldsExportForm
from unfold.views import UnfoldModelAdminViewMixin

from django.db.models import F, Sum, Count, ExpressionWrapper, DurationField
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView
from unfold.decorators import display

from production.models import ProductionItemSession, ProductionItemSessionStatus
from django import forms
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.widgets import UnfoldAdminDateWidget
from django.views.generic import ListView

class EmployeeSessionsFilterForm(forms.Form):
    from_date = forms.DateField(
        required=False,
        widget=UnfoldAdminDateWidget
    )
    to_date = forms.DateField(
        required=False,
        widget=UnfoldAdminDateWidget
    )

    class Media:
        js = [
            f"admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]


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


@admin.register(Employee)
class EmployeeAdmin(ModelAdmin, CompanyRestrictedAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    export_form_class = SelectableFieldsExportForm

    change_form_template = "admin/company/employee/change_form.html"

    list_display = (
        "full_name",
        "company",
        "is_active",
        "user",
    )

    list_filter = (
        "company",
        "is_active",
    )

    search_fields = (
        "first_name",
        "last_name",
        "email",
        "employee_id",
        "user__username",
        "user__email",
    )

    autocomplete_fields = (
        "company",
        "user",
    )

    ordering = (
        "company",
        "last_name",
        "first_name",
    )

    fieldsets = (
        (
            _("Employee"),
            {
                "fields": (
                    ("first_name", "last_name"),
                    ("email", "phone"),
                    "user",
                )
            },
        ),
        (
            _("Personal Information"),
            {
                "fields": (
                    "identification_number",
                    "birth_date",
                    ("sex", "civil_status"),
                    "birth_country",
                )
            },
        ),
        (
            _("Employment"),
            {
                "fields": (
                    "company",
                    ("hired_at", "terminated_at"),
                    "is_active",
                )
            },
        ),
    )

    readonly_fields = ()

    def full_name(self, obj):
        return obj.full_name

    full_name.short_description = "Name"

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["employee_sessions_url"] = reverse(
            "admin:company_employee_sessions",
            args=[object_id],
        )
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def get_urls(self):
        return [
            path(
                "<int:employee_id>/sessions/",
                self.admin_site.admin_view(EmployeeSessionsView.as_view(model_admin=self)),
                name="company_employee_sessions",
            ),
        ] + super().get_urls()