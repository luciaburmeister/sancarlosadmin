from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.decorators import action, display

from payroll.models.nomina import (
    PayrollPeriod,
    PayrollConcept,
    EmployeePayroll,
    PayrollConceptLine,
    PayrollSSBreakdown,
    PayrollImportPendingRow,
)
from payroll.services.period_service import close_period, reopen_period
from payroll.views import PayrollImportAdminView, PayrollPendingResolveView


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
        "pending_count_badge",
    )
    list_filter = ("status", "source", "company", "year")
    search_fields = ("company__name",)
    autocomplete_fields = ("company",)
    ordering = ("-year", "-month")

    actions_list = ["import_a3_action"]
    actions_detail = ["close_period_action", "reopen_period_action"]
    actions_row = ["close_period_action", "reopen_period_action"]

    readonly_fields = (
        "closed_at",
        "total_net_salary",
        "total_employer_ss",
        "total_employee_ss",
        "total_irpf_withheld",
        "total_employer_cost",
        "salary_payment_date",
        "ss_payment_date",
    )

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
        (
            _("Totals & payment dates (computed on close)"),
            {
                "fields": (
                    ("total_net_salary", "total_employer_cost"),
                    ("total_employer_ss", "total_employee_ss"),
                    ("total_irpf_withheld",),
                    ("salary_payment_date", "ss_payment_date"),
                ),
            },
        ),
    )

    inlines = [EmployeePayrollInline]

    @action(description=_("Close period"), icon="lock", permissions=["close"])
    def close_period_action(self, request, object_id):
        period = get_object_or_404(PayrollPeriod, pk=object_id)
        try:
            close_period(period)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(
                request,
                _("Period closed. Totals and payment dates were computed."),
            )
        return redirect(reverse("admin:payroll_payrollperiod_change", args=[object_id]))

    def has_close_permission(self, request, object_id=None):
        return request.user.has_perm("payroll.change_payrollperiod")

    @action(description=_("Reopen period"), icon="lock_open", permissions=["reopen"])
    def reopen_period_action(self, request, object_id):
        period = get_object_or_404(PayrollPeriod, pk=object_id)
        try:
            reopen_period(period)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, _("Period reopened."))
        return redirect(reverse("admin:payroll_payrollperiod_change", args=[object_id]))

    def has_reopen_permission(self, request, object_id=None):
        return request.user.has_perm("payroll.change_payrollperiod")

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

    @display(description=_("Pending"), label={"ok": "success", "pending": "danger"})
    def pending_count_badge(self, obj):
        count = obj.pending_count
        if count == 0:
            return "ok", _("None")
        return "pending", _("%(n)d to resolve") % {"n": count}

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "import-a3/",
                self.admin_site.admin_view(
                    PayrollImportAdminView.as_view(model_admin=self)
                ),
                name="payroll_payroll_import",
            ),
        ]
        return custom + urls

    @action(
        description=_("Import from A3"),
        url_path="import-a3-action",
        permissions=["import_a3"],
        icon="upload_file",
    )
    def import_a3_action(self, request):
        return redirect(reverse("admin:payroll_payroll_import"))

    def has_import_a3_permission(self, request):
        return request.user.has_perm("payroll.add_employeepayroll")


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


@admin.register(PayrollImportPendingRow)
class PayrollImportPendingRowAdmin(ModelAdmin):
    list_display = (
        "full_name",
        "dni",
        "a3_code",
        "period",
        "show_reason",
        "net_salary",
        "gross_salary",
    )
    list_filter = ("reason", "period__company", "period__year")
    search_fields = ("full_name", "a3_code", "dni")
    ordering = ("period", "full_name")
    actions_row = ["resolve_row_action"]

    readonly_fields = (
        "period",
        "a3_code",
        "dni",
        "full_name",
        "reason",
        "net_salary",
        "gross_salary",
        "employer_cost",
        "source_file",
        "payload",
    )

    @display(
        description=_("Reason"),
        label={
            PayrollImportPendingRow.Reason.UNMATCHED: "warning",
            PayrollImportPendingRow.Reason.AMBIGUOUS: "danger",
        },
    )
    def show_reason(self, obj):
        return obj.reason

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/resolve/",
                self.admin_site.admin_view(
                    PayrollPendingResolveView.as_view(model_admin=self)
                ),
                name="payroll_payrollimportpendingrow_resolve",
            ),
        ]
        return custom + urls

    @action(
        description=_("Resolve"),
        url_path="resolve-row-action",
        permissions=["resolve"],
        icon="how_to_reg",
    )
    def resolve_row_action(self, request, object_id):
        return redirect(
            reverse("admin:payroll_payrollimportpendingrow_resolve", args=[object_id])
        )

    def has_resolve_permission(self, request, object_id=None):
        return request.user.has_perm("payroll.add_employeepayroll")