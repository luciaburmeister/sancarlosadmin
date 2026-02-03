from django.contrib import admin
from unfold.admin import ModelAdmin

from company.models import Employee, EmployeeType
from django.utils.translation import gettext_lazy as _
from core.admin.company_admin import CompanyRestrictedAdmin
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ExportForm, ImportForm, SelectableFieldsExportForm

@admin.register(Employee)
class EmployeeAdmin(ModelAdmin,CompanyRestrictedAdmin,ImportExportModelAdmin):
    import_form_class = ImportForm
    export_form_class = ExportForm
    export_form_class = SelectableFieldsExportForm
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
                ("type"),
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

@admin.register(EmployeeType)
class EmployeeTypeAdmin(ModelAdmin):
    list_display = ("type", "company")
    search_fields = ("type",)
