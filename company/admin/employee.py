from django.contrib import admin
from unfold.admin import ModelAdmin

from django.urls import path, reverse
from company.models import Employee
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm, SelectableFieldsExportForm

from django.db.models import F, Sum, Count, ExpressionWrapper, DurationField
from unfold.decorators import display
from company.views.employeesessions import EmployeeSessionsView


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