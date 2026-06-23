from django.db import models
from django.utils.translation import gettext_lazy as _
from timestamps.models import Timestampable

from payroll.models.convenio import CollectiveAgreementCategory


class EmployeePayrollProfile(Timestampable):
    """
    Payroll-specific data for an employee.
    Mirrors A3's employee maintenance tabs:
    Affiliation, Contributions, Contracts.
    One record per employee.
    """

    employee = models.OneToOneField(
        "company.Employee",
        on_delete=models.CASCADE,
        related_name="payroll_profile",
        verbose_name=_("Employee"),
    )

    a3_employee_code = models.CharField(
        max_length=20,
        blank=True,
        db_index=True,
        verbose_name=_("A3 Employee Code"),
        help_text=_("Sequential employee code in A3 (e.g. 000001). Used to match payroll imports."),
    )

    social_security_affiliation_number = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("SS Affiliation Number"),
        help_text=_("Número de Afiliación a la Seguridad Social"),
    )

    date_of_birth = models.DateField(null=True, blank=True, verbose_name=_("Date of Birth"))

    class Gender(models.TextChoices):
        MALE = "male", _("Male")
        FEMALE = "female", _("Female")

    gender = models.CharField(
        max_length=10,
        choices=Gender.choices,
        blank=True,
        verbose_name=_("Gender"),
    )

    class MaritalStatus(models.TextChoices):
        SINGLE = "single", _("Single")
        MARRIED = "married", _("Married")
        DIVORCED = "divorced", _("Divorced")
        WIDOWED = "widowed", _("Widowed")
        SEPARATED = "separated", _("Separated")

    marital_status = models.CharField(
        max_length=20,
        choices=MaritalStatus.choices,
        blank=True,
        verbose_name=_("Marital Status"),
    )

    nationality_code = models.CharField(
        max_length=3,
        default="724",
        verbose_name=_("Nationality Code"),
        help_text=_("724 = Spain"),
    )

    category = models.ForeignKey(
        CollectiveAgreementCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="employees",
        verbose_name=_("Professional Category"),
    )

    contribution_group = models.CharField(
        max_length=5,
        blank=True,
        verbose_name=_("Contribution Group"),
        help_text=_("SS contribution group (1-11)"),
    )

    tgss_occupation_code = models.CharField(
        max_length=10,
        blank=True,
        verbose_name=_("TGSS Occupation Code"),
    )

    class PaymentFrequency(models.TextChoices):
        MONTHLY = "monthly", _("Monthly")
        WEEKLY = "weekly", _("Weekly")
        BIWEEKLY = "biweekly", _("Bi-weekly")

    payment_frequency = models.CharField(
        max_length=20,
        choices=PaymentFrequency.choices,
        default=PaymentFrequency.MONTHLY,
        verbose_name=_("Payment Frequency"),
    )

    multiple_employment = models.BooleanField(default=False, verbose_name=_("Multiple Employment"))

    is_part_time = models.BooleanField(default=False, verbose_name=_("Part-time"))

    part_time_coefficient = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        default=0,
        verbose_name=_("Part-time Coefficient"),
        help_text=_("e.g. 0.500 for half-time"),
    )

    hire_date = models.DateField(null=True, blank=True, verbose_name=_("Hire Date"))

    seniority_date = models.DateField(null=True, blank=True, verbose_name=_("Seniority Date"))

    category_seniority_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Category Seniority Date"),
    )

    company_seniority_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Company Seniority Date"),
    )

    extra_pay_calculation_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Extra Pay Calculation Date"),
    )

    termination_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Termination Date"),
    )

    termination_reason = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Termination Reason"),
    )

    class IRPFKey(models.TextChoices):
        A = "A", _("Employees (A)")
        B = "B", _("Business owners (B)")
        C = "C", _("Farmers (C)")
        D = "D", _("Public figures (D)")
        E = "E", _("Other (E)")

    irpf_key = models.CharField(
        max_length=5,
        choices=IRPFKey.choices,
        default=IRPFKey.A,
        verbose_name=_("IRPF Key"),
    )

    class TaxResidency(models.TextChoices):
        RESIDENT = "resident", _("Resident")
        NON_RESIDENT = "non_resident", _("Non-resident")

    tax_residency = models.CharField(
        max_length=20,
        choices=TaxResidency.choices,
        default=TaxResidency.RESIDENT,
        verbose_name=_("Tax Residency"),
    )

    class IRPFType(models.TextChoices):
        AUTOMATIC = "automatic", _("Automatic")
        MANUAL = "manual", _("Manual")

    irpf_type = models.CharField(
        max_length=20,
        choices=IRPFType.choices,
        default=IRPFType.AUTOMATIC,
        verbose_name=_("IRPF Type"),
    )

    irpf_base_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=100,
        verbose_name=_("IRPF Base Percentage"),
    )

    withholding_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name=_("Withholding Percentage"),
    )

    annual_income = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        verbose_name=_("Annual Income"),
    )

    number_of_children = models.PositiveSmallIntegerField(default=0, verbose_name=_("Number of Children"))

    iban = models.CharField(
        max_length=34,
        blank=True,
        verbose_name=_("IBAN"),
    )

    swift_bic = models.CharField(
        max_length=11,
        blank=True,
        verbose_name=_("SWIFT/BIC"),
    )

    class Meta:
        verbose_name = _("Employee Payroll Profile")
        verbose_name_plural = _("Employee Payroll Profiles")

    def __str__(self):
        return f"{self.employee} — Payroll Profile"