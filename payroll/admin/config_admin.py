from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from payroll.models.company_config import CompanyPayrollConfig
from payroll.models.employee_profile import EmployeePayrollProfile


@admin.register(CompanyPayrollConfig)
class CompanyPayrollConfigAdmin(ModelAdmin):
    list_display = (
        "company",
        "collective_agreement",
        "number_of_salary_payments",
        "cnae",
        "social_security_registration_number",
    )
    search_fields = ("company__name", "cnae", "social_security_registration_number")
    autocomplete_fields = ("company", "collective_agreement")

    fieldsets = (
        (
            _("Company"),
            {
                "fields": (
                    "company",
                    "collective_agreement",
                )
            },
        ),
        (
            _("Identification"),
            {
                "fields": (
                    ("social_security_registration_number", "cnae"),
                    "occupational_accident_insurance_provider",
                )
            },
        ),
        (
            _("Temporary Disability / Voluntary Supplement"),
            {
                "fields": (
                    ("temporary_disability_percentage", "voluntary_ss_supplement_percentage"),
                    ("use_temporary_disability_from_collective_agreement", "temporary_disability_complement"),
                    "irpf_withholding_table",
                )
            },
        ),
        (
            _("Payments"),
            {
                "fields": (
                    "number_of_salary_payments",
                    ("pay_by_bank_transfer", "ss_payment_iban"),
                )
            },
        ),
        (
            _("Vacations & Contracts"),
            {
                "fields": (
                    ("vacation_days", "vacation_accrual_start"),
                    "round_vacation_days",
                    (
                        "temporary_contract_severance_days",
                        "severance_subject_to_irpf",
                    ),
                )
            },
        ),
        (
            _("Sick Leave & Accidents"),
            {
                "fields": (
                    ("sick_leave_type", "workplace_accident_type"),
                    ("maternity_paternity_type", "pregnancy_risk_type"),
                )
            },
        ),
        (
            _("Payslip"),
            {
                "fields": (
                    ("payslip_format", "payslip_type"),
                    ("hide_bank_details", "include_back_pay"),
                    "payroll_incidents_cutoff_day",
                )
            },
        ),
    )


@admin.register(EmployeePayrollProfile)
class EmployeePayrollProfileAdmin(ModelAdmin):
    list_display = (
        "employee",
        "social_security_affiliation_number",
        "category",
        "contribution_group",
        "payment_frequency",
        "hire_date",
        "is_part_time",
    )
    search_fields = (
        "employee__first_name",
        "employee__last_name",
        "social_security_affiliation_number",
    )
    list_filter = ("payment_frequency", "is_part_time", "tax_residency", "irpf_key")
    autocomplete_fields = ("employee", "category")

    fieldsets = (
        (
            _("Employee"),
            {
                "fields": ("employee",)
            },
        ),
        (
            _("Personal Data"),
            {
                "fields": (
                    "social_security_affiliation_number",
                    ("date_of_birth", "gender"),
                    ("marital_status", "nationality_code"),
                )
            },
        ),
        (
            _("Social Security Contribution"),
            {
                "fields": (
                    "category",
                    ("contribution_group", "tgss_occupation_code"),
                    "payment_frequency",
                    "multiple_employment",
                    ("is_part_time", "part_time_coefficient"),
                )
            },
        ),
        (
            _("Dates"),
            {
                "fields": (
                    ("hire_date", "seniority_date"),
                    ("category_seniority_date", "company_seniority_date"),
                    "extra_pay_calculation_date",
                    ("termination_date", "termination_reason"),
                )
            },
        ),
        (
            _("IRPF"),
            {
                "fields": (
                    ("irpf_key", "tax_residency", "irpf_type"),
                    ("irpf_base_percentage", "withholding_percentage"),
                    ("annual_income", "number_of_children"),
                )
            },
        ),
        (
            _("Bank"),
            {
                "fields": (
                    "iban",
                    "swift_bic",
                )
            },
        ),
    )