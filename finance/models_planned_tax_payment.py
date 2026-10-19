from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.models.fields import MoneyField
from simple_history.models import HistoricalRecords
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel
from payments.mixins import Payable, PaidVia


class PlannedTaxPaymentQuerySet(models.QuerySet):
    def delete(self):
        from .models import TaxPeriod

        period_ids = set(TaxPeriod.objects.filter(planned_payments__in=self).values_list("pk", flat=True))
        deleted = super().delete()
        if period_ids:
            transaction.on_commit(lambda: TaxPeriod.recalculate_status_for(period_ids))
        return deleted


class PlannedTaxPayment(Timestampable, CompanyOwnedModel, Payable):
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
            "Optional reference periods this payment covers. Leave blank for "
            "payments with no calculated period (deferrals, IBI, penalties). "
            "Link several when one payment settles multiple periods together "
            "(e.g. a year-end VAT settlement netting quarters that came out "
            "positive and negative)."
        ),
    )
    description = models.CharField(
        max_length=200,
        verbose_name=_("Description"),
    )
    amount = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Amount"),
        help_text=_("Positive = payment to the tax authority. Negative = refund expected back."),
    )
    payment_date = models.DateField(verbose_name=_("Payment Date"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    is_paid = models.BooleanField(default=False, verbose_name=_("Paid"))
    paid_via = models.CharField(
        max_length=20, choices=PaidVia.choices, blank=True, default="",
        verbose_name=_("Paid Via"),
        help_text=_("Empty until the payment is actually paid."),
    )
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Paid At"))
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("Paid By"),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Planned Tax Payment")
        verbose_name_plural = _("Planned Tax Payments")
        ordering = ["-payment_date"]

    def __str__(self):
        return f"{self.description} — {self.amount} ({self.payment_date})"

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
    def is_refund(self) -> bool:
        return bool(self.amount) and self.amount.amount < 0

    @property
    def amount_paid(self):
        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Sum
        from djmoney.money import Money
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(self)
        total = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or 0
        )
        return Money(total, self.amount.currency)

    @property
    def amount_remaining(self):
        target = abs(self.amount.amount) if self.amount else 0
        return target - self.amount_paid.amount

    def recalculate_payment_status(self):
        from .models import TaxPeriod

        total_applied = self.amount_paid.amount
        target = abs(self.amount.amount) if self.amount else 0
        fully_covered = target > 0 and total_applied >= target

        if fully_covered and not self.is_paid:
            self.is_paid = True
            self.paid_via = PaidVia.RECONCILIATION
            self.paid_at = timezone.now()
            self.paid_by = None
            self.save(update_fields=["is_paid", "paid_via", "paid_at", "paid_by", "updated_at"])
        elif not fully_covered and self.is_paid and self.paid_via == PaidVia.RECONCILIATION:
            self.is_paid = False
            self.paid_via = ""
            self.paid_at = None
            self.paid_by = None
            self.save(update_fields=["is_paid", "paid_via", "paid_at", "paid_by", "updated_at"])

        TaxPeriod.recalculate_status_for(self.tax_periods.values_list("pk", flat=True))

    def mark_as_paid(self, user):
        self.is_paid = True
        self.paid_at = timezone.now()
        self.paid_by = user
        self.paid_via = PaidVia.MANUAL
        self.save(update_fields=["is_paid", "paid_at", "paid_by", "paid_via", "updated_at"])

    def mark_as_unpaid(self):
        self.is_paid = False
        self.paid_at = None
        self.paid_by = None
        self.paid_via = ""
        self.save(update_fields=["is_paid", "paid_at", "paid_by", "paid_via", "updated_at"])

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
