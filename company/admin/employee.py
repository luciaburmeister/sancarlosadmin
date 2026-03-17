from unfold.admin import StackedInline
from django.contrib import admin
from unfold.admin import ModelAdmin
from django.db import models

from django.urls import path, reverse
from company.models import Employee, EmployeeType, EmployeeDocument, EmployeeDocumentType
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.forms.widgets import ArrayWidget
from django.contrib.postgres.fields import ArrayField
from unfold.contrib.import_export.forms import (
    ExportForm,
    ImportForm,
    SelectableFieldsExportForm,
)
from company.views.employeesessions import EmployeeSessionsView

from company.forms import BaseDocumentForm


class EmployeeDocumentForm(BaseDocumentForm):
    class Meta:
        model = EmployeeDocument
        fields = "__all__"


class EmployeeDocumentInline(StackedInline):
    model = EmployeeDocument
    form = EmployeeDocumentForm
    extra = 0
    tab = True
    exclude = ["company"]
    autocomplete_fields = ["document_type"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type",  "file"),
                ("does_not_expire","is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )

@admin.register(EmployeeDocumentType)
class EmployeeDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

@admin.register(Employee)
class EmployeeAdmin(ModelAdmin, CompanyRestrictedAdmin, ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    export_form_class = SelectableFieldsExportForm

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

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
                "classes": ["tab"],
                "fields": (
                    ("first_name", "last_name"),
                    ("email", "phone"),
                    ("type"),
                    "user",
                )
            },
        ),
        (
            _("Personal Information"),
            {
                "classes": ["tab"],
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
                "classes": ["tab"],
                "fields": (
                    "company",
                    ("hired_at", "terminated_at"),
                    "is_active",
                )
            },
        ),
        (
            _("Production"),
            {
                "classes": ["tab"],
                "fields": (
                    ("can_work_on_production", "production_stages"),
                )
            },
        ),
    )

    inlines = [EmployeeDocumentInline]

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)
        if obj and obj.company:
            stages = obj.company.production_stages
            if stages:
                form.base_fields["production_stages"].widget = ArrayWidget(
                    choices=[(s, s) for s in stages]
                )
        return form

    readonly_fields = ()

    def full_name(self, obj):
        return obj.full_name
    full_name.short_description = _("Name")

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


@admin.register(EmployeeType)
class EmployeeTypeAdmin(ModelAdmin):
    list_display = ("type", "company")
    search_fields = ("type",)
