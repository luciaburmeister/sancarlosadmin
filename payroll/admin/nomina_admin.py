from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.decorators import display

from payroll.models.nomina import (
    PayrollPeriod,
    PayrollConcept,
    EmployeePayroll,
    PayrollConceptLine,
    PayrollSSBreakdown,
)


class PayrollConceptLineInline(TabularInline):
    model = PayrollConceptLine
    extra = 0
    tab = True
    fields = ("concept", "quantity", "unit_price", "amount", "notes")
    autocomplete_fields = ("concept",)
    verbose_name = _("Concept Line")
    verbose_name_plural = _("Accruals & Deductions")


class PayrollSSBreakdownInline(TabularInline):
    model = PayrollSSBreakdown
    extra = 0
    tab = True
    fields = (
        "concept",
        "base",
        "employee_rate",
        "employee_amount",
        "employer_rate",
        "employer_amount",
    )
    verbose_name = _("Social Security Concept")
    verbose_name_plural = _("Social Security Breakdown")


class EmployeePayrollInline(StackedInline):
    model = EmployeePayroll
    extra = 0
    show_change_link = True
    tab = True
    fields = (
        "employee",
        "category",
        "days_in_period",
        "gross_salary",
        "net_salary",
        "employer_cost",
        "social_security_base",
        "irpf_base",
        "irpf_percentage",
    )
    readonly_fields = ("employee",)
    verbose_name = _("Employee Payroll")
    verbose_name_plural = _("Employee Payrolls")


@admin.register(PayrollPeriod)
class PayrollPeriodAdmin(ModelAdmin):
    list_display = (
        "company",
        "month",
        "year",
        "show_status",
        "show_source",
        "closed_at",
        "payroll_count",
    )
    list_filter = ("status", "source", "company", "year")
    search_fields = ("company__name",)
    autocomplete_fields = ("company",)
    ordering = ("-year", "-month")

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "company",
                    ("year", "month"),
                    ("status", "source"),
                    "closed_at",
                    "notes",
                )
            },
        ),
    )

    inlines = [EmployeePayrollInline]

    @display(
        description=_("Status"),
        label={
            PayrollPeriod.Status.DRAFT: "warning",
            PayrollPeriod.Status.CLOSED: "success",
        },
    )
    def show_status(self, obj):
        return obj.status

    @display(
        description=_("Source"),
        label={
            PayrollPeriod.Source.MANUAL: "info",
            PayrollPeriod.Source.EXCEL_IMPORT: "success",
            PayrollPeriod.Source.API: "warning",
            PayrollPeriod.Source.AI: "danger",
        },
    )
    def show_source(self, obj):
        return obj.source

    @display(description=_("Payrolls"))
    def payroll_count(self, obj):
        return obj.payrolls.count()


@admin.register(PayrollConcept)
class PayrollConceptAdmin(ModelAdmin):
    list_display = (
        "code",
        "name",
        "show_type",
        "is_salary",
        "counts_towards_social_security",
        "counts_towards_irpf",
    )
    list_filter = ("type", "is_salary", "counts_towards_social_security", "counts_towards_irpf")
    search_fields = ("code", "name")
    ordering = ("code",)

    fieldsets = (
        (
            None,
            {
                "fields": (
                    ("code", "name"),
                    "type",
                    ("is_salary", "counts_towards_social_security", "counts_towards_irpf"),
                )
            },
        ),
    )

    @display(
        description=_("Type"),
        label={
            PayrollConcept.ConceptType.ACCRUAL: "success",
            PayrollConcept.ConceptType.DEDUCTION: "danger",
        },
    )
    def show_type(self, obj):
        return obj.type


@admin.register(EmployeePayroll)
class EmployeePayrollAdmin(ModelAdmin):
    list_display = (
        "employee",
        "period",
        "company",
        "category",
        "days_in_period",
        "gross_salary",
        "net_salary",
        "employer_cost",
        "irpf_percentage",
    )
    list_filter = (
        "period__year",
        "period__month",
        "company",
    )
    search_fields = (
        "employee__first_name",
        "employee__last_name",
        "social_security_affiliation_number",
    )
    autocomplete_fields = ("period", "employee", "company")
    ordering = ("-period__year", "-period__month", "employee__last_name")

    fieldsets = (
        (
            _("Header"),
            {
                "fields": (
                    ("period", "employee", "company"),
                    ("category", "social_security_affiliation_number"),
                    ("contract_type", "days_in_period"),
                    ("period_start", "period_end"),
                    "iban",
                )
            },
        ),
        (
            _("Totals"),
            {
                "fields": (
                    ("gross_salary", "prorated_extra_payments"),
                    ("total_accrued", "total_deducted"),
                    ("net_salary", "employer_cost"),
                )
            },
        ),
        (
            _("Bases"),
            {
                "fields": (
                    ("social_security_base", "workplace_accident_base", "irpf_base"),
                )
            },
        ),
        (
            _("Deductions"),
            {
                "fields": (
                    ("employee_social_security", "employer_social_security"),
                    ("irpf_withholding_amount", "irpf_percentage"),
                )
            },
        ),
        (
            _("Import Metadata"),
            {
                "fields": (
                    "source_file",
                    "raw_data",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    inlines = [PayrollConceptLineInline, PayrollSSBreakdownInline]