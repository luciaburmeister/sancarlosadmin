from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from timestamps.models import Timestampable

from payments.mixins import Payable


class PayrollPeriod(Timestampable):
    """
    A closed payroll month for a company.
    One per company per month.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="payroll_periods",
        verbose_name=_("Company"),
    )

    year = models.PositiveSmallIntegerField(verbose_name=_("Year"))

    month = models.PositiveSmallIntegerField(
        verbose_name=_("Month"),
        choices=[(i, i) for i in range(1, 13)],
    )

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        CLOSED = "closed", _("Closed")

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        verbose_name=_("Status"),
    )

    class Source(models.TextChoices):
        MANUAL = "manual", _("Manual Entry")
        EXCEL_IMPORT = "excel_import", _("Excel Import (A3)")
        API = "api", _("External API")
        AI = "ai", _("AI Import")

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.MANUAL,
        verbose_name=_("Data Source"),
    )

    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Closed At"),
    )

    # ── Aggregated totals (computed when the period is closed) ──
    total_net_salary = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        verbose_name=_("Total Net Salary"),
        help_text=_("Sum of employee net pay. Computed on close."),
    )

    total_employer_ss = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        verbose_name=_("Total Employer Social Security"),
        help_text=_("Sum of employer SS contributions. Computed on close."),
    )

    total_employee_ss = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        verbose_name=_("Total Employee Social Security"),
        help_text=_("Sum of employee SS contributions withheld. Computed on close."),
    )

    total_irpf_withheld = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        verbose_name=_("Total IRPF Withheld"),
        help_text=_("Sum of IRPF withholdings. Computed on close."),
    )

    total_employer_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        verbose_name=_("Total Employer Cost"),
        help_text=_("Total company cost. Computed on close."),
    )

    # ── Expected payment dates (for cashflow projection) ──
    salary_payment_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Salary Payment Date"),
        help_text=_("Expected date salaries leave the bank."),
    )

    ss_payment_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Social Security Payment Date"),
        help_text=_("Expected date the TGSS settlement is paid."),
    )

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Payroll Period")
        verbose_name_plural = _("Payroll Periods")
        ordering = ["-year", "-month"]
        unique_together = [("company", "year", "month")]

    def __str__(self):
        return f"{self.company} — {self.month:02d}/{self.year}"

    @property
    def pending_count(self):
        if not self.pk:
            return 0
        return self.pending_rows.count()

    def clean(self):
        super().clean()
        # B: a period cannot be closed while there are unaccounted employees.
        if self.status == self.Status.CLOSED and self.pk and self.pending_rows.exists():
            raise ValidationError(
                _(
                    "Cannot close this period: %(n)d employee(s) from the import are "
                    "still unmatched. Resolve them first."
                )
                % {"n": self.pending_rows.count()}
            )


class PayrollConcept(Timestampable):
    """
    Master catalog of payroll concepts.
    Mirrors A3 concept codes (125, 995, etc.).
    """

    code = models.CharField(
        max_length=10,
        unique=True,
        verbose_name=_("Code"),
        help_text=_("A3 concept code e.g. 125, 995"),
    )

    name = models.CharField(max_length=100, verbose_name=_("Name"))

    class ConceptType(models.TextChoices):
        ACCRUAL = "accrual", _("Accrual")
        DEDUCTION = "deduction", _("Deduction")

    type = models.CharField(
        max_length=20,
        choices=ConceptType.choices,
        verbose_name=_("Type"),
    )

    is_salary = models.BooleanField(
        default=True,
        verbose_name=_("Is Salary"),
        help_text=_("Salary-based vs non-salary compensation"),
    )

    counts_towards_social_security = models.BooleanField(
        default=True,
        verbose_name=_("Counts Towards SS Base"),
    )

    counts_towards_irpf = models.BooleanField(
        default=True,
        verbose_name=_("Counts Towards IRPF Base"),
    )

    class Meta:
        verbose_name = _("Payroll Concept")
        verbose_name_plural = _("Payroll Concepts")
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"


class EmployeePayroll(Timestampable, Payable):
    """
    One payroll record per employee per period.
    Stores all figures from the payslip.
    """

    period = models.ForeignKey(
        PayrollPeriod,
        on_delete=models.CASCADE,
        related_name="payrolls",
        verbose_name=_("Period"),
    )

    employee = models.ForeignKey(
        "company.Employee",
        on_delete=models.PROTECT,
        related_name="payrolls",
        verbose_name=_("Employee"),
    )

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="payrolls",
        verbose_name=_("Company"),
    )

    category = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Category"),
    )

    social_security_affiliation_number = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("SS Affiliation Number"),
    )

    contract_type = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Contract Type"),
    )

    days_in_period = models.PositiveSmallIntegerField(
        default=30,
        verbose_name=_("Days in Period"),
    )

    period_start = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Period Start"),
    )

    period_end = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Period End"),
    )

    gross_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Gross Salary"),
    )

    prorated_extra_payments = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Prorated Extra Payments"),
    )

    net_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Net Salary"),
    )

    employer_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Employer Cost"),
    )

    social_security_base = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Social Security Base"),
    )

    workplace_accident_base = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Workplace Accident / Occupational Disease Base"),
    )

    irpf_base = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("IRPF Base"),
    )

    total_accrued = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Total Accrued"),
    )

    total_deducted = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Total Deducted"),
    )

    employee_social_security = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Employee SS Contribution"),
    )

    irpf_withholding_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("IRPF Withholding Amount"),
    )

    irpf_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name=_("IRPF Percentage"),
    )

    employer_social_security = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Employer SS Contribution"),
    )

    iban = models.CharField(
        max_length=34,
        blank=True,
        verbose_name=_("IBAN"),
    )

    payment_day_override = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Payment Day Override"),
        help_text=_(
            "Day of the month this employee is expected to be paid, for cashflow. "
            "Leave empty to inherit the company's "
            "configured day."
        ),
    )

    is_paid = models.BooleanField(default=False, verbose_name=_("Paid"))

    paid_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Paid At"),
    )

    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("Paid By"),
    )

    class PaidVia(models.TextChoices):
        MANUAL = "manual", _("Manual")
        RECONCILIATION = "reconciliation", _("Bank Reconciliation")

    paid_via = models.CharField(
        max_length=20,
        choices=PaidVia.choices,
        default=PaidVia.MANUAL,
        verbose_name=_("Paid Via"),
        help_text=_(
            "Whether this was marked paid manually or automatically via "
            "bank reconciliation."
        ),
    )

    raw_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Raw Import Data"),
        help_text=_("Original row from Excel/API import for audit trail"),
    )

    source_file = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Source File"),
    )

    class Meta:
        verbose_name = _("Employee Payroll")
        verbose_name_plural = _("Employee Payrolls")
        ordering = ["-period__year", "-period__month", "employee__last_name"]
        unique_together = [("period", "employee")]

    def __str__(self):
        return f"{self.employee} — {self.period}"

    @property
    def amount_remaining(self) -> Decimal:
        """Payable contract: net salary while unpaid, zero once confirmed paid."""
        return Decimal("0.00") if self.is_paid else self.net_salary

    @property
    def counterparty_key(self) -> tuple:
        """Reimbursement/payment goes to the employee, not a provider/client."""
        return ("employee", self.employee_id)

    def recalculate_paid_status(self) -> None:
        """
        Mirrors ExpenseClaim.recalculate_reimbursement() for payroll: sums this
        payroll's PaymentAllocations and, if they now cover the full net
        salary, marks it paid automatically (paid_via=RECONCILIATION, paid_by
        left null since no human clicked anything). If a previously-covering
        allocation is removed (PaymentAllocation post_delete), reverts is_paid
        back to False — but ONLY when paid_via is RECONCILIATION, never
        overriding a manual "mark as paid" done from the admin.
        """
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(self)
        total_applied = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or Decimal("0.00")
        )

        if total_applied >= self.net_salary and not self.is_paid:
            self.is_paid = True
            self.paid_at = timezone.now()
            self.paid_by = None
            self.paid_via = self.PaidVia.RECONCILIATION
            self.save(update_fields=["is_paid", "paid_at", "paid_by", "paid_via", "updated_at"])
        elif total_applied < self.net_salary and self.is_paid and self.paid_via == self.PaidVia.RECONCILIATION:
            self.is_paid = False
            self.paid_at = None
            self.paid_by = None
            self.paid_via = self.PaidVia.MANUAL
            self.save(update_fields=["is_paid", "paid_at", "paid_by", "paid_via", "updated_at"])


class PayrollConceptLine(Timestampable):
    """
    Individual accrual or deduction line within a payroll.
    Mirrors the concept rows on the payslip.
    """

    payroll = models.ForeignKey(
        EmployeePayroll,
        on_delete=models.CASCADE,
        related_name="concept_lines",
        verbose_name=_("Payroll"),
    )

    concept = models.ForeignKey(
        PayrollConcept,
        on_delete=models.PROTECT,
        related_name="lines",
        verbose_name=_("Concept"),
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_("Amount"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Quantity"),
        help_text=_("For hours, days, units, etc."),
    )

    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=5,
        null=True,
        blank=True,
        verbose_name=_("Unit Price"),
    )

    notes = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Notes"),
    )

    class Meta:
        verbose_name = _("Payroll Concept Line")
        verbose_name_plural = _("Payroll Concept Lines")
        ordering = ["concept__code"]

    def __str__(self):
        return f"{self.concept} — {self.amount}"


class PayrollSSBreakdown(Timestampable):
    """
    Detailed Social Security contribution breakdown.
    Mirrors the lower section of the Spanish payslip:
    'Determinación de las Bases de Cotización a la Seguridad Social...'
    """

    payroll = models.ForeignKey(
        EmployeePayroll,
        on_delete=models.CASCADE,
        related_name="ss_breakdown",
        verbose_name=_("Payroll"),
    )

    class SSConcept(models.TextChoices):
        COMMON_CONTINGENCIES = "common_contingencies", _("Common Contingencies")
        MEI = "mei", _("MEI (Intergenerational Equity Mechanism)")
        WORKPLACE_ACCIDENTS_AND_OCCUPATIONAL_DISEASES = "at_ep", _("Workplace Accidents and Occupational Diseases")
        UNEMPLOYMENT = "unemployment", _("Unemployment")
        VOCATIONAL_TRAINING = "fp", _("Vocational Training")
        FOGASA = "fogasa", _("FOGASA")
        FORCE_MAJEURE_OVERTIME = "force_majeure_overtime", _("Force Majeure Overtime")
        OTHER_OVERTIME = "other_overtime", _("Other Overtime")
        SOLIDARITY_CONTRIBUTION = "solidarity", _("Additional Solidarity Contribution")

    concept = models.CharField(
        max_length=30,
        choices=SSConcept.choices,
        verbose_name=_("Social Security Concept"),
    )

    base = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Base"),
    )

    employee_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=0,
        verbose_name=_("Employee Rate Percentage"),
    )

    employee_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Employee Amount"),
    )

    employer_rate = models.DecimalField(
        max_digits=6,
        decimal_places=4,
        default=0,
        verbose_name=_("Employer Rate Percentage"),
    )

    employer_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Employer Amount"),
    )

    class Meta:
        verbose_name = _("Social Security Breakdown")
        verbose_name_plural = _("Social Security Breakdowns")
        ordering = ["concept"]
        unique_together = [("payroll", "concept")]

    def __str__(self):
        return f"{self.get_concept_display()} — {self.base}"


class PayrollImportPendingRow(Timestampable):
    """
    An employee column from an A3 import that could NOT be matched to an
    ERP employee (no A3 code on profile / unknown name, or ambiguous match).

    The full parsed payload is stored so the row can be resolved later from
    the admin (assigning it to an employee) without re-uploading the file.
    A period with pending rows cannot be closed (see ``PayrollPeriod.clean``).
    """

    period = models.ForeignKey(
        PayrollPeriod,
        on_delete=models.CASCADE,
        related_name="pending_rows",
        verbose_name=_("Period"),
    )

    a3_code = models.CharField(max_length=20, blank=True, verbose_name=_("A3 Employee Code"))
    dni = models.CharField(max_length=20, blank=True, verbose_name=_("DNI / NIF"))
    full_name = models.CharField(max_length=200, verbose_name=_("Name in File"))
    surname1 = models.CharField(max_length=100, blank=True, verbose_name=_("Surname 1"))
    surname2 = models.CharField(max_length=100, blank=True, verbose_name=_("Surname 2"))
    given_name = models.CharField(max_length=100, blank=True, verbose_name=_("Given Name"))

    class Reason(models.TextChoices):
        UNMATCHED = "unmatched", _("No matching employee")
        AMBIGUOUS = "ambiguous", _("Ambiguous (multiple matches)")

    reason = models.CharField(
        max_length=20,
        choices=Reason.choices,
        default=Reason.UNMATCHED,
        verbose_name=_("Reason"),
    )

    net_salary = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name=_("Net Salary")
    )
    gross_salary = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name=_("Gross Salary")
    )
    employer_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name=_("Employer Cost")
    )

    payload = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Parsed Payload"),
        help_text=_("Full parsed figures, used to build the payroll on resolution."),
    )

    source_file = models.CharField(max_length=255, blank=True, verbose_name=_("Source File"))

    class Meta:
        verbose_name = _("Pending Payroll Row")
        verbose_name_plural = _("Pending Payroll Rows")
        ordering = ["period", "full_name"]
        unique_together = [("period", "a3_code", "dni")]

    def __str__(self):
        return f"{self.full_name or self.a3_code or self.dni} — {self.period}"