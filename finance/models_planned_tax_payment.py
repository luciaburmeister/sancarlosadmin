from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from simple_history.models import HistoricalRecords
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel
from payments.mixins import Payable


class TaxInstallmentPaymentStatus(models.TextChoices):
    UNPAID = "unpaid", _("Unpaid")
    PARTIAL = "partial", _("Partial")
    PAID = "paid", _("Paid")


class PlannedTaxPaymentQuerySet(models.QuerySet):
    def delete(self):
        from .models import TaxPeriod

        period_ids = set(TaxPeriod.objects.filter(planned_payments__in=self).values_list("pk", flat=True))
        deleted = super().delete()
        if period_ids:
            transaction.on_commit(lambda: TaxPeriod.recalculate_status_for(period_ids))
        return deleted


class PlannedTaxPayment(Timestampable, CompanyOwnedModel):
    objects = PlannedTaxPaymentQuerySet.as_manager()

    tax_schedule_rule = models.ForeignKey(
        "finance.TaxScheduleRule",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="planned_payments",
        verbose_name=_("Tax Schedule Rule"),
        help_text=_(
            "Optional. When set, company and tax type are derived from the rule, "
            "and linked tax periods must belong to it. Leave blank for payments "
            "with no calculated rule (deferrals, IBI, penalties)."
        ),
    )
    tax_type = models.ForeignKey(
        "invoices.TaxType",
        on_delete=models.PROTECT,
        related_name="planned_tax_payments",
        verbose_name=_("Tax Type"),
    )
    tax_periods = models.ManyToManyField(
        "finance.TaxPeriod",
        blank=True,
        related_name="planned_payments",
        verbose_name=_("Tax Periods"),
        help_text=_(
            "Periods covered by this payment. Leave blank for deferrals, IBI, "
            "or penalties with no calculated period."
        ),
    )
    description = models.CharField(
        max_length=200,
        verbose_name=_("Description"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    drag_forward_if_unpaid = models.BooleanField(
        default=True,
        verbose_name=_("Drag Forward If Unpaid"),
        help_text=_(
            "If yes, an unpaid installment keeps reappearing on today's date "
            "in the cashflow calendar until paid or reconciled."
        ),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Planned Tax Payment")
        verbose_name_plural = _("Planned Tax Payments")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.description} — {self.tax_type}"

    def clean(self):
        super().clean()
        if self.pk:
            if not self.tax_schedule_rule_id:
                if self.tax_periods.exists():
                    raise ValidationError(
                        _("Tax periods can only be linked when a tax schedule rule is selected.")
                    )
            else:
                mismatched = self.tax_periods.exclude(rule_id=self.tax_schedule_rule_id)
                if mismatched.exists():
                    raise ValidationError(
                        _("All linked tax periods must belong to the selected tax schedule rule.")
                    )

    @property
    def payment_status(self) -> str:
        statuses = set(self.installments.values_list("payment_status", flat=True))
        if not statuses or statuses == {TaxInstallmentPaymentStatus.UNPAID}:
            return TaxInstallmentPaymentStatus.UNPAID
        if statuses == {TaxInstallmentPaymentStatus.PAID}:
            return TaxInstallmentPaymentStatus.PAID
        return TaxInstallmentPaymentStatus.PARTIAL

    def save(self, *args, **kwargs):
        if self.tax_schedule_rule_id:
            self.company_id = self.tax_schedule_rule.company_id
            self.tax_type_id = self.tax_schedule_rule.tax_type_id
        super().save(*args, **kwargs)
        from .models import TaxPeriod

        TaxPeriod.recalculate_status_for(self.tax_periods.values_list("pk", flat=True))

    def delete(self, *args, **kwargs):
        from .models import TaxPeriod

        period_ids = list(self.tax_periods.values_list("pk", flat=True))
        result = super().delete(*args, **kwargs)
        if period_ids:
            transaction.on_commit(lambda: TaxPeriod.recalculate_status_for(period_ids))
        return result


class TaxInstallment(Payable, models.Model):
    planned_tax_payment = models.ForeignKey(
        PlannedTaxPayment,
        on_delete=models.CASCADE,
        related_name="installments",
        verbose_name=_("Planned Tax Payment"),
    )
    due_date = models.DateField(verbose_name=_("Due Date"))
    amount = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Amount"),
        validators=[MinValueValidator(Money(0, "EUR"))],
        help_text=_(
            "Always positive - the amount owed, like Invoice or LoanInstallment. "
            "Not a bank movement: the matching BankTransaction is negative "
            "(money leaving the account) when this installment gets paid."
        ),
    )
    amount_paid = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Amount Paid"),
        help_text=_("Maintained automatically from PaymentAllocation. Not manually editable."),
    )
    payment_status = models.CharField(
        max_length=10,
        choices=TaxInstallmentPaymentStatus.choices,
        default=TaxInstallmentPaymentStatus.UNPAID,
        verbose_name=_("Payment Status"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Tax Installment")
        verbose_name_plural = _("Tax Installments")
        ordering = ["planned_tax_payment", "due_date"]
        unique_together = ["planned_tax_payment", "due_date"]

    def __str__(self):
        return f"{self.planned_tax_payment} — {self.due_date}"

    @property
    def amount_remaining(self):
        return self.amount - self.amount_paid

    @property
    def counterparty_key(self) -> tuple:
        return ("tax_type", self.planned_tax_payment.tax_type_id)

    def recalculate_payment_status(self):
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(TaxInstallment)
        allocated = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"] or 0
        )
        total = self.amount.amount if self.amount else 0

        self.amount_paid = Money(allocated, self.amount_currency)

        if allocated <= 0:
            new_status = TaxInstallmentPaymentStatus.UNPAID
        elif allocated >= total:
            new_status = TaxInstallmentPaymentStatus.PAID
        else:
            new_status = TaxInstallmentPaymentStatus.PARTIAL

        self.payment_status = new_status
        self.save(update_fields=["amount_paid", "amount_paid_currency", "payment_status"])

    def save(self, *args, **kwargs):
        if not kwargs.get("update_fields") and self.pk:
            self.recalculate_payment_status()
            return
        super().save(*args, **kwargs)
