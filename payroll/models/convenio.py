from django.db import models
from django.utils.translation import gettext_lazy as _
from timestamps.models import Timestampable


class CollectiveAgreement(Timestampable):
    official_code = models.CharField(
        max_length=20,
        unique=True,
        verbose_name=_("Official Code"),
    )

    description = models.CharField(
        max_length=200,
        verbose_name=_("Description"),
    )

    publication_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Publication Date"),
    )

    valid_from = models.DateField(verbose_name=_("Valid From"))

    valid_until = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Valid Until"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Currently Active"))

    class ProfessionalClassification(models.TextChoices):
        CATEGORY = "category", _("Category")
        GROUP = "group", _("Group")
        LEVEL = "level", _("Level")

    professional_classification = models.CharField(
        max_length=20,
        choices=ProfessionalClassification.choices,
        default=ProfessionalClassification.CATEGORY,
        verbose_name=_("Professional Classification"),
    )

    seniority_by_classification = models.BooleanField(
        default=False,
        verbose_name=_("Seniority by Classification"),
    )

    extra_payments_level = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("Extra Payments Level"),
    )

    seniority_level = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("Seniority Level"),
    )

    class Meta:
        verbose_name = _("Collective Agreement")
        verbose_name_plural = _("Collective Agreements")
        ordering = ["description"]

    def __str__(self):
        return f"{self.official_code} — {self.description}"


class CollectiveAgreementCategory(Timestampable):
    collective_agreement = models.ForeignKey(
        CollectiveAgreement,
        on_delete=models.CASCADE,
        related_name="categories",
        verbose_name=_("Collective Agreement"),
    )

    code = models.CharField(max_length=10, verbose_name=_("Code"))

    official_name = models.CharField(max_length=100, verbose_name=_("Official Name"))

    base_salary = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=0,
        verbose_name=_("Base Salary"),
    )

    collective_agreement_allowance = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=0,
        verbose_name=_("Collective Agreement Allowance"),
    )

    contribution_group = models.CharField(
        max_length=5,
        blank=True,
        verbose_name=_("Contribution Group"),
    )

    tgss_occupation_code = models.CharField(
        max_length=10,
        blank=True,
        verbose_name=_("TGSS Occupation Code"),
    )

    temporary_disability_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name=_("Temporary Disability %"),
    )

    voluntary_ss_supplement_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name=_("Voluntary SS Supplement %"),
    )

    probation_period_months = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_("Probation Period (Months)"),
    )

    class Meta:
        verbose_name = _("Collective Agreement Category")
        verbose_name_plural = _("Collective Agreement Categories")
        ordering = ["collective_agreement", "code"]
        unique_together = [("collective_agreement", "code")]

    def __str__(self):
        return f"{self.code} — {self.official_name}"


class CollectiveAgreementExtraPay(Timestampable):
    collective_agreement = models.ForeignKey(
        CollectiveAgreement,
        on_delete=models.CASCADE,
        related_name="extra_payments",
        verbose_name=_("Collective Agreement"),
    )

    number = models.PositiveSmallIntegerField(verbose_name=_("Number"))

    name = models.CharField(
        max_length=50,
        verbose_name=_("Name"),
    )

    class ExtraPayType(models.TextChoices):
        ORDINARY = "ordinary", _("Ordinary")
        PROFIT_SHARING = "profit_sharing", _("Profit Sharing")

    type = models.CharField(
        max_length=20,
        choices=ExtraPayType.choices,
        default=ExtraPayType.ORDINARY,
        verbose_name=_("Type"),
    )

    payment_date = models.CharField(
        max_length=5,
        verbose_name=_("Payment Date"),
        help_text=_("Format: DD/MM e.g. 20/06"),
    )

    accrual_start = models.CharField(max_length=30, verbose_name=_("Accrual Start"))
    accrual_end = models.CharField(max_length=30, verbose_name=_("Accrual End"))

    temporary_disability_discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=100,
        verbose_name=_("Temporary Disability Discount %"),
    )

    include_in_payslip = models.BooleanField(
        default=False,
        verbose_name=_("Include in Payslip"),
    )

    breakdown_concepts = models.BooleanField(
        default=True,
        verbose_name=_("Break Down Concepts"),
    )

    deduct_sick_leave = models.BooleanField(
        default=True,
        verbose_name=_("Deduct Sick Leave"),
    )

    deduct_workplace_accident = models.BooleanField(
        default=False,
        verbose_name=_("Deduct Workplace Accident"),
    )

    deduct_maternity_paternity = models.BooleanField(
        default=True,
        verbose_name=_("Deduct Maternity / Paternity"),
    )

    deduct_unjustified_absence = models.BooleanField(
        default=True,
        verbose_name=_("Deduct Unjustified Absence"),
    )

    prorate_on_hire_and_termination = models.BooleanField(
        default=True,
        verbose_name=_("Prorate on Hire / Termination"),
    )

    import_collective_agreement_data = models.BooleanField(
        default=True,
        verbose_name=_("Import Collective Agreement Data"),
    )

    class Meta:
        verbose_name = _("Extra Payment")
        verbose_name_plural = _("Extra Payments")
        ordering = ["collective_agreement", "number"]
        unique_together = [("collective_agreement", "number")]

    def __str__(self):
        return f"{self.name} ({self.collective_agreement})"


class CollectiveAgreementSeniority(Timestampable):
    collective_agreement = models.ForeignKey(
        CollectiveAgreement,
        on_delete=models.CASCADE,
        related_name="seniority_table",
        verbose_name=_("Collective Agreement"),
    )

    year = models.PositiveSmallIntegerField(verbose_name=_("Year"))

    percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Percentage"),
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Amount"),
    )

    class Meta:
        verbose_name = _("Seniority Table")
        verbose_name_plural = _("Seniority Tables")
        ordering = ["collective_agreement", "year"]
        unique_together = [("collective_agreement", "year")]

    def __str__(self):
        return f"{self.collective_agreement} — Year {self.year}"