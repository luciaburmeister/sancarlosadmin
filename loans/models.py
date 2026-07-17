import logging
from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from django_countries.fields import CountryField
from simple_history.models import HistoricalRecords
from payments.mixins import Payable
from company.models import CompanyOwnedModel, BankAccount
from timestamps.models import Timestampable

logger = logging.getLogger(__name__)


class InterestRateType(models.TextChoices):
    FIXED = "fixed", _("Fixed")
    VARIABLE = "variable", _("Variable")


class AmortizationMethod(models.TextChoices):
    FRENCH = "french", _("French (constant installment)")
    GERMAN = "german", _("German (constant principal)")
    BULLET = "bullet", _("Bullet (interest-only)")


class InterestDayCount(models.TextChoices):
    ACT_360 = "act_360", _("ACT/360")
    THIRTY_360 = "30_360", _("30/360")


class PaymentFrequency(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    QUARTERLY = "quarterly", _("Quarterly")
    ANNUAL = "annual", _("Annual")


class LoanStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ACTIVE = "active", _("Active")
    SETTLED = "settled", _("Settled")
    DEFAULTED = "defaulted", _("Defaulted")
    CANCELLED = "cancelled", _("Cancelled")


class LoanInstallmentPaymentStatus(models.TextChoices):
    UNPAID = "unpaid", _("Unpaid")
    PARTIAL = "partial", _("Partial")
    PAID = "paid", _("Paid")


class LoanEventType(models.TextChoices):
    EARLY_REPAYMENT = "early_repayment", _("Early Repayment")
    PENALTY = "penalty", _("Penalty")
    RESTRUCTURE = "restructure", _("Restructure")
    DISBURSEMENT = "disbursement", _("Disbursement")


class Bank(models.Model):
    """Global bank catalog, shared across all companies."""
    name = models.CharField(max_length=150, verbose_name=_("Name"))
    arcopay_service = models.CharField(
        max_length=100, unique=True, blank=True, null=True,
        verbose_name=_("Arcopay Service Code"),
    )
    swift_bic = models.CharField(max_length=11, blank=True, verbose_name=_("SWIFT/BIC"))
    country = CountryField(blank=True, null=True, verbose_name=_("Country"))
    logo_url = models.URLField(blank=True, verbose_name=_("Logo URL"))
    notes = models.TextField(blank=True, null=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Bank")
        verbose_name_plural = _("Banks")

    def __str__(self):
        return self.name


class ParentLoan(CompanyOwnedModel, Timestampable):
    bank = models.ForeignKey(
        Bank,
        on_delete=models.PROTECT,
        related_name="parent_loans",
        verbose_name=_("Bank"),
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name="parent_loans",
        verbose_name=_("Bank Account"),
        help_text=_("Company account this loan is disbursed to / paid from."),
    )
    name = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Name"),
        help_text=_("Optional label, e.g. 'Leasing máquina 1'."),
    )
    reference = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("Reference"),
    )
    notes = models.TextField(blank=True, null=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Parent Loan")
        verbose_name_plural = _("Parent Loans")
        ordering = ["-created_at"]

    def __str__(self):
        label = self.name or self.reference or str(self.pk)
        return f"{label} - {self.bank}"

    @property
    def active_tranche(self):
        return self.tranches.filter(status=LoanStatus.ACTIVE).first()

    @property
    def total_outstanding(self):
        tranche = self.active_tranche
        if tranche:
            return tranche.outstanding_principal
        return None


class Loan(CompanyOwnedModel, Timestampable):
    FINANCIAL_FIELDS = (
        "principal_amount", "principal_amount_currency", "interest_rate",
        "amortization_method", "interest_day_count", "term_months",
        "payment_frequency", "start_date",
    )

    parent_loan = models.ForeignKey(
        ParentLoan,
        on_delete=models.PROTECT,
        related_name="tranches",
        verbose_name=_("Parent Loan"),
        null=True,
        blank=True,
    )
    bank = models.ForeignKey(
        Bank,
        on_delete=models.PROTECT,
        related_name="loans",
        verbose_name=_("Bank"),
    )
    name = models.CharField(
        max_length=150, blank=True, null=True, verbose_name=_("Name"),
        help_text=_("Optional label to identify this loan, e.g. 'Leasing máquina 1'."),
    )
    reference = models.CharField(
        max_length=100, blank=True, null=True, verbose_name=_("Reference"),
    )
    principal_amount = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Principal Amount"),
    )
    interest_rate = models.DecimalField(
        max_digits=6, decimal_places=3, verbose_name=_("Interest Rate"),
        help_text=_(
            "Choose annual nominal rate, e.g. 5 for 5%. "
            "Annual rate = Monthly rate × 12."
        ),
    )
    interest_rate_type = models.CharField(
        max_length=10, choices=InterestRateType.choices,
        default=InterestRateType.FIXED, verbose_name=_("Interest Rate Type"),
    )
    amortization_method = models.CharField(
        max_length=10, choices=AmortizationMethod.choices,
        verbose_name=_("Amortization Method"),
    )
    interest_day_count = models.CharField(
        max_length=10, choices=InterestDayCount.choices,
        default=InterestDayCount.ACT_360, verbose_name=_("Interest Day Count"),
        help_text=_(
            "ACT/360: calculated on actual days between payments, "
            "standard used by most Spanish banks. | "
            "30/360: assumes every month has 30 days. "
            "Use ONLY if your contract explicitly states it."
        ),
    )
    start_date = models.DateField(verbose_name=_("Start Date"))
    term_months = models.PositiveIntegerField(verbose_name=_("Term (months)"))
    payment_frequency = models.CharField(
        max_length=10, choices=PaymentFrequency.choices,
        default=PaymentFrequency.MONTHLY, verbose_name=_("Payment Frequency"),
    )
    status = models.CharField(
        max_length=10, choices=LoanStatus.choices,
        default=LoanStatus.DRAFT, verbose_name=_("Status"),
    )
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name="loans",
        verbose_name=_("Bank Account"),
        help_text=_("Company account this loan is disbursed to / paid from."),
    )
    notes = models.TextField(blank=True, null=True, verbose_name=_("Notes"))

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Loan")
        verbose_name_plural = _("Loans")
        ordering = ["-start_date"]

    def __str__(self):
        label = self.name or self.reference or str(self.pk)
        return f"{label} - {self.bank}"

    @property
    def has_locked_schedule(self) -> bool:
        return self.installments.exclude(
            payment_status=LoanInstallmentPaymentStatus.UNPAID
        ).exists()

    @property
    def outstanding_principal(self):
        last_paid = (
            self.installments
            .filter(payment_status=LoanInstallmentPaymentStatus.PAID)
            .order_by("-installment_number")
            .first()
        )
        if last_paid:
            return last_paid.outstanding_principal_after
        return self.principal_amount

    @property
    def remaining_months(self):
        return self.installments.filter(
            payment_status__in=[
                LoanInstallmentPaymentStatus.UNPAID,
                LoanInstallmentPaymentStatus.PARTIAL,
            ]
        ).count()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        is_new = self._state.adding

        changed_financial_fields = False
        if not is_new:
            previous = Loan.objects.filter(pk=self.pk).first()
            if previous:
                changed_financial_fields = any(
                    getattr(previous, f) != getattr(self, f)
                    for f in self.FINANCIAL_FIELDS
                )

        should_generate = (
            (update_fields is None or "status" in update_fields)
            and self.status == LoanStatus.ACTIVE
        )
        should_sync_account = (
            self.bank_account_id
            and (is_new or update_fields is None or "bank_account" in update_fields)
        )

        super().save(*args, **kwargs)

        self._pending_stale_schedule_warning = False

        if should_generate and not self.installments.exists():
            from django.db import transaction
            from loans.services.amortization import generate_schedule
            transaction.on_commit(lambda: generate_schedule(self))
        elif changed_financial_fields and self.installments.exists():
            from loans.services.amortization import generate_schedule
            if self.has_locked_schedule:
                self._pending_stale_schedule_warning = True
            else:
                generate_schedule(self, force=True)

        if should_sync_account:
            self._queue_bank_account_sync()

    def _queue_bank_account_sync(self):
        if not self.bank_account_id:
            return
        try:
            from banking.tasks import sync_bank_account_task
            sync_bank_account_task(self.bank_account_id)
        except Exception:
            logger.warning(
                "Could not queue bank account sync for loan %s",
                self.pk,
                exc_info=True,
            )


class LoanEvent(Timestampable, Payable):
    """
    Records events outside the regular amortization schedule.
    Early repayments are created automatically by EarlyRepaymentView.
    Other event types (penalty, restructure, disbursement) are manual.

    Implements Payable so PaymentAllocation can be linked to it,
    enabling full reconciliation against a BankTransaction.
    """
    parent_loan = models.ForeignKey(
        ParentLoan,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("Parent Loan"),
    )
    event_type = models.CharField(
        max_length=20,
        choices=LoanEventType.choices,
        verbose_name=_("Event Type"),
    )
    date = models.DateField(verbose_name=_("Date"))
    amount = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        null=True, blank=True,
        verbose_name=_("Amount"),
        help_text=_("Capital amount paid. Leave blank for restructure events."),
    )
    notes = models.TextField(blank=True, null=True, verbose_name=_("Notes"))

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Loan Event")
        verbose_name_plural = _("Loan Events")
        ordering = ["date"]

    def __str__(self):
        return f"{self.parent_loan} — {self.get_event_type_display()} ({self.date})"

    # --- Payable interface ---

    @property
    def amount_remaining(self):
        if not self.amount:
            return Money(0, "EUR")
        from django.contrib.contenttypes.models import ContentType
        from payments.models import PaymentAllocation
        ct = ContentType.objects.get_for_model(LoanEvent)
        allocated = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"] or Decimal("0")
        )
        return Money(self.amount.amount - allocated, self.amount_currency)

    @property
    def amount_remaining_decimal(self):
        return self.amount_remaining.amount

    @property
    def counterparty_key(self) -> tuple:
        return ("bank", self.parent_loan.bank_id)

    def recalculate_payment_status(self):
        """Called by payments/_recalculate_invoice_payment_status when a PaymentAllocation is saved."""
        pass  # LoanEvent has no payment_status field — reconciliation state is derived


class LoanInstallment(Payable, models.Model):
    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name="installments",
        verbose_name=_("Loan"),
    )
    installment_number = models.PositiveIntegerField(verbose_name=_("Installment Number"))
    due_date = models.DateField(verbose_name=_("Due Date"))
    principal_due = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Principal Due"),
    )
    interest_due = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Interest Due"),
    )
    total_due = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Total Due"),
    )
    amount_paid = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Amount Paid"),
        help_text=_("Maintained automatically from PaymentAllocation. Not manually editable."),
    )
    payment_status = models.CharField(
        max_length=10,
        choices=LoanInstallmentPaymentStatus.choices,
        default=LoanInstallmentPaymentStatus.UNPAID,
        verbose_name=_("Payment Status"),
    )
    outstanding_principal_after = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Outstanding Principal"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Loan Installment")
        verbose_name_plural = _("Loan Installments")
        ordering = ["loan", "installment_number"]
        unique_together = ["loan", "installment_number"]

    def __str__(self):
        return f"{self.loan} - #{self.installment_number}"

    @property
    def amount_remaining(self):
        return self.total_due - self.amount_paid

    @property
    def counterparty_key(self) -> tuple:
        return ("bank", self.loan.bank_id)

    def recalculate_payment_status(self):
        from django.contrib.contenttypes.models import ContentType
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(LoanInstallment)
        allocated = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"] or 0
        )
        total = self.total_due.amount if self.total_due else 0

        self.amount_paid = Money(allocated, self.total_due_currency)

        if allocated <= 0:
            new_status = LoanInstallmentPaymentStatus.UNPAID
        elif allocated >= total:
            new_status = LoanInstallmentPaymentStatus.PAID
        else:
            new_status = LoanInstallmentPaymentStatus.PARTIAL

        self.payment_status = new_status
        self.save(update_fields=["amount_paid", "amount_paid_currency", "payment_status"])

    def save(self, *args, **kwargs):
        if not kwargs.get("update_fields") and self.pk:
            self.recalculate_payment_status()
            return
        super().save(*args, **kwargs)