from django.db import models
from django.utils.translation import gettext_lazy as _
from timestamps.models import Timestampable

from payroll.models.convenio import CollectiveAgreement


class CompanyPayrollConfig(Timestampable):
    """
    Payroll configuration per company.
    Mirrors A3's 'Mantenimiento Datos Centro'.
    One record per company.
    """

    company = models.OneToOneField(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="payroll_config",
        verbose_name=_("Company"),
    )

    collective_agreement = models.ForeignKey(
        CollectiveAgreement,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="companies",
        verbose_name=_("Collective Agreement"),
    )

    social_security_registration_number = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("SS Registration Number"),
        help_text=_("SS Contribution Account Code (CCC)"),
    )

    cnae = models.CharField(
        max_length=10,
        blank=True,
        verbose_name=_("CNAE"),
    )

    occupational_accident_insurance_provider = models.CharField(
        max_length=10,
        blank=True,
        verbose_name=_("Occupational Accident Insurance Provider"),
    )

    temporary_disability_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0.65,
        verbose_name=_("Temporary Disability Percentage"),
        help_text=_("Percentage Temporary Disability"),
    )

    voluntary_ss_supplement_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=1.00,
        verbose_name=_("Voluntary SS Supplement Percentage"),
    )

    use_temporary_disability_from_collective_agreement = models.BooleanField(
        default=True,
        verbose_name=_("Import IT from Collective Agreement"),
    )

    temporary_disability_complement = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Temporary Disability Complement"),
    )

    irpf_withholding_table = models.PositiveSmallIntegerField(
        default=1,
        verbose_name=_("IRPF Withholding Table"),
    )

    number_of_salary_payments = models.PositiveSmallIntegerField(
        default=14,
        choices=[(12, "12 payments"), (14, "14 payments")],
        verbose_name=_("Number of Salary Payments"),
    )

    vacation_days = models.PositiveSmallIntegerField(
        default=30,
        verbose_name=_("Vacation Days"),
    )

    vacation_accrual_start = models.CharField(
        max_length=5,
        default="1/01",
        verbose_name=_("Vacation Accrual Start"),
    )

    round_vacation_days = models.BooleanField(
        default=False,
        verbose_name=_("Round Vacation Days"),
    )

    temporary_contract_severance_days = models.PositiveSmallIntegerField(
        default=8,
        verbose_name=_("Temporary Contract Severance Days"),
    )

    severance_subject_to_irpf = models.BooleanField(
        default=False,
        verbose_name=_("Severance Subject to IRPF"),
    )

    class BenefitType(models.TextChoices):
        REGULATORY_BASE = "regulatory_base", _("Regulatory Base (R)")
        NO_SUPPLEMENT = "no_supplement", _("No Supplement (N)")

    sick_leave_type = models.CharField(
        max_length=30,
        choices=BenefitType.choices,
        default=BenefitType.REGULATORY_BASE,
        verbose_name=_("Sick Leave Type"),
    )

    workplace_accident_type = models.CharField(
        max_length=30,
        choices=BenefitType.choices,
        default=BenefitType.REGULATORY_BASE,
        verbose_name=_("Workplace Accident Type"),
    )

    maternity_paternity_type = models.CharField(
        max_length=30,
        choices=BenefitType.choices,
        default=BenefitType.NO_SUPPLEMENT,
        verbose_name=_("Maternity / Paternity Type"),
    )

    pregnancy_risk_type = models.CharField(
        max_length=30,
        choices=BenefitType.choices,
        default=BenefitType.REGULATORY_BASE,
        verbose_name=_("Pregnancy Risk Type"),
    )

    class PayslipFormat(models.TextChoices):
        PLAIN_PAPER = "plain_paper", _("Plain Paper / Laser")
        PREPRINTED = "pre_printed", _("Pre-printed")

    payslip_format = models.CharField(
        max_length=30,
        choices=PayslipFormat.choices,
        default=PayslipFormat.PLAIN_PAPER,
        verbose_name=_("Payslip Format"),
    )

    class PayslipType(models.TextChoices):
        RECEIPT = "receipt", _("Receipt")
        PAYROLL = "payroll", _("Payroll")

    payslip_type = models.CharField(
        max_length=20,
        choices=PayslipType.choices,
        default=PayslipType.RECEIPT,
        verbose_name=_("Payslip Type"),
    )

    hide_bank_details = models.BooleanField(
        default=True,
        verbose_name=_("Hide Bank Details on Payslip"),
    )

    include_back_pay = models.BooleanField(
        default=True,
        verbose_name=_("Include Back Pay"),
    )

    pay_by_bank_transfer = models.BooleanField(
        default=True,
        verbose_name=_("Pay by Bank Transfer"),
    )

    ss_payment_iban = models.CharField(
        max_length=34,
        blank=True,
        verbose_name=_("IBAN for Social Security Payments"),
    )

    payroll_incidents_cutoff_day = models.PositiveSmallIntegerField(
        default=31,
        verbose_name=_("Payroll Incidents Cutoff Day"),
    )

    class Meta:
        verbose_name = _("Company Payroll Configuration")
        verbose_name_plural = _("Company Payroll Configurations")

    def __str__(self):
        return f"{self.company} — Payroll Configuration"