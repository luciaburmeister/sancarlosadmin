from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from djmoney.models.fields import MoneyField
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel


class PeriodUnit(models.TextChoices):
    DAYS   = "days",   _("Days")
    MONTHS = "months", _("Months")
    YEARS  = "years",  _("Years")


class TaxPeriodStatus(models.TextChoices):
    OPEN            = "open",            _("Open")
    PENDING_PAYMENT = "pending_payment", _("Pending Payment")
    PAID            = "paid",            _("Paid")
    OVERDUE         = "overdue",         _("Overdue")


class TaxScheduleRule(Timestampable, CompanyOwnedModel):
    name = models.CharField(
        max_length=150,
        verbose_name=_("Rule Name"),
        help_text=_("E.g. 'Quarterly VAT', 'Monthly IRPF'"),
    )
    tax_type = models.ForeignKey(
        "invoices.TaxType",
        on_delete=models.PROTECT,
        related_name="schedule_rules",
        verbose_name=_("Tax Type"),
        help_text=_("Invoices with this tax type will be accumulated automatically into periods."),
    )
    start_date = models.DateField(
        verbose_name=_("Start Date"),
        help_text=_("Date on which the first accumulation period begins."),
        default="2026-01-01",
    )
    period_length = models.PositiveIntegerField(
        verbose_name=_("Period Length"),
        help_text=_("Number of units per period. E.g. 3 + Months = quarterly, 1 + Month = monthly."),
        validators=[MinValueValidator(1)],
    )
    period_unit = models.CharField(
        max_length=10,
        choices=PeriodUnit.choices,
        default=PeriodUnit.MONTHS,
        verbose_name=_("Period Unit"),
    )
    payment_day = models.PositiveIntegerField(
        verbose_name=_("Payment Day"),
        help_text=_("Day of the month the payment is due. E.g. 20 → due on the 20th."),
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    meses_hasta_vencimiento = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Months Until Due"),
        help_text=_(
            "Months from the period close to the payment due date. "
            "E.g. Quarterly VAT — period closes March 31, "
            "with 1 month offset the payment is due April 20."
        ),
        validators=[MinValueValidator(1)],
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Inactive rules will not generate new periods."),
    )

    class Meta:
        verbose_name = _("Tax Schedule Rule")
        verbose_name_plural = _("Tax Schedule Rules")
        ordering = ["company", "name"]

    def __str__(self):
        return f"{self.name} ({self.company})"

    REGEN_FIELDS = {"start_date", "period_length", "period_unit", "payment_day", "meses_hasta_vencimiento"}

    def save(self, *args, **kwargs):
        is_new = self.pk is None

        changed = set()
        if not is_new:
            try:
                old = TaxScheduleRule.objects.get(pk=self.pk)
                changed = {f for f in self.REGEN_FIELDS if getattr(old, f) != getattr(self, f)}
            except TaxScheduleRule.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        if not self.is_active:
            return

        if is_new or (changed & self.REGEN_FIELDS):
            TaxPeriod.objects.filter(
                rule=self,
                status=TaxPeriodStatus.OPEN,
            ).delete()

        self.generate_up_to_today()

    def _compute_due_date(self, period_end):
        """Calculate due date from period end date."""
        import calendar
        from dateutil.relativedelta import relativedelta
        target = period_end + relativedelta(months=self.meses_hasta_vencimiento)
        max_day = calendar.monthrange(target.year, target.month)[1]
        return target.replace(day=min(self.payment_day, max_day))

    def create_next_period(self, from_date=None):
        """
        Create a single period starting from `from_date`.
        Uses get_or_create so it is safe to call multiple times.
        """
        from dateutil.relativedelta import relativedelta

        start = from_date or self.start_date
        end = start + relativedelta(**{self.period_unit: self.period_length}) - relativedelta(days=1)
        due_date = self._compute_due_date(end)

        period, _ = TaxPeriod.objects.get_or_create(
            rule=self,
            date_from=start,
            defaults={
                "company": self.company,
                "date_to": end,
                "due_date": due_date,
                "status": TaxPeriodStatus.OPEN,
            },
        )
        return period

    def compute_all_periods(self):
        """
        Compute all periods for this rule in chronological order.
        Ensures accumulated_credit is always correct since each
        period depends on the tax_net of all prior periods.
        """
        periods = TaxPeriod.objects.filter(rule=self).order_by("date_from")
        for period in periods:
            period.compute(save=True)

    def generate_up_to_today(self):
        """
        Generate ALL missing periods from start_date up to and including
        the period that covers today. Safe to call repeatedly — uses
        get_or_create so existing periods are never overwritten.
        """
        from dateutil.relativedelta import relativedelta
        from django.utils import timezone

        today = timezone.now().date()
        start = self.start_date

        while start <= today:
            end = start + relativedelta(**{self.period_unit: self.period_length}) - relativedelta(days=1)
            due_date = self._compute_due_date(end)

            TaxPeriod.objects.get_or_create(
                rule=self,
                date_from=start,
                defaults={
                    "company": self.company,
                    "date_to": end,
                    "due_date": due_date,
                    "status": TaxPeriodStatus.OPEN,
                },
            )
            start = end + relativedelta(days=1)


class TaxPeriod(Timestampable, CompanyOwnedModel):
    rule      = models.ForeignKey(TaxScheduleRule, on_delete=models.PROTECT, related_name="periods", verbose_name=_("Tax Schedule Rule"))
    date_from = models.DateField(verbose_name=_("Period Start"))
    date_to   = models.DateField(verbose_name=_("Period End"))
    due_date  = models.DateField(verbose_name=_("Due Date"))
    status    = models.CharField(
        max_length=20,
        choices=TaxPeriodStatus.choices,
        default=TaxPeriodStatus.OPEN,
        verbose_name=_("Status"),
    )

    tax_collected = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Tax Collected"),
        help_text=_("Total tax aggregated from sales invoices in this period."),
    )
    tax_paid = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Tax Paid"),
        help_text=_("Total tax aggregated from purchase invoices in this period."),
    )
    tax_net = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Tax Net"),
        help_text=_("Tax Collected − Tax Paid. Amount owed to the tax authority."),
    )
    accumulated_credit = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Accumulated Credit"),
        help_text=_("Negative tax accumulated from prior periods of the same rule. Offset against future positive results."),
    )
    net_after_credit = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Net to Pay"),
        help_text=_("Tax net after applying accumulated credit. If negative, the excess carries forward to the next period."),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Tax Period")
        verbose_name_plural = _("Tax Periods")
        ordering = ["-date_from"]
        unique_together = [["rule", "date_from"]]

    def __str__(self):
        return f"{self.rule.tax_type} | {self.date_from} → {self.date_to}"

    def compute(self, save: bool = True):
        from django.contrib.contenttypes.models import ContentType
        from invoices.models import AppliedTax, SalesInvoice, Invoice
        from django.db.models import Sum
        from decimal import Decimal

        tax_type = self.rule.tax_type

        from invoices.models import SalesInvoiceStatus
        sales_ct = ContentType.objects.get_for_model(SalesInvoice)
        sales_ids = list(SalesInvoice.objects.filter(
            company=self.company,
            date_issued__range=(self.date_from, self.date_to),
            status=SalesInvoiceStatus.FINAL,
        ).values_list("id", flat=True))

        collected = Decimal("0.00")
        if sales_ids:
            result = (
                AppliedTax.objects.filter(
                    content_type=sales_ct,
                    object_id__in=sales_ids,
                    tax_type=tax_type,
                    calculated_amount__isnull=False,
                ).aggregate(t=Sum("calculated_amount"))["t"]
            )
            if result is not None:
                collected = Decimal(str(result.amount if hasattr(result, "amount") else result))

        purchase_ct = ContentType.objects.get_for_model(Invoice)
        purchase_ids = list(Invoice.objects.filter(
            company=self.company,
            accounting_date__range=(self.date_from, self.date_to),
        ).values_list("id", flat=True))

        paid = Decimal("0.00")
        if purchase_ids:
            result = (
                AppliedTax.objects.filter(
                    content_type=purchase_ct,
                    object_id__in=purchase_ids,
                    tax_type=tax_type,
                    calculated_amount__isnull=False,
                ).aggregate(t=Sum("calculated_amount"))["t"]
            )
            if result is not None:
                paid = Decimal(str(result.amount if hasattr(result, "amount") else result))

        self.tax_collected = collected
        self.tax_paid = paid
        self.tax_net = collected - paid

        prior_periods = TaxPeriod.objects.filter(
            rule=self.rule,
            date_from__lt=self.date_from,
        ).exclude(pk=self.pk if self.pk else 0)

        accumulated = Decimal("0.00")
        for p in prior_periods:
            net = Decimal(str(p.tax_net.amount if hasattr(p.tax_net, "amount") else p.tax_net))
            if net < 0:
                accumulated += abs(net)

        self.accumulated_credit = accumulated
        net_val = collected - paid
        net_after = net_val - accumulated
        self.net_after_credit = net_after

        if save:
            self.save(update_fields=[
                "tax_collected", "tax_collected_currency",
                "tax_paid", "tax_paid_currency",
                "tax_net", "tax_net_currency",
                "accumulated_credit", "accumulated_credit_currency",
                "net_after_credit", "net_after_credit_currency",
            ])


class TaxPayment(Timestampable, CompanyOwnedModel):
    period       = models.ForeignKey(TaxPeriod, on_delete=models.PROTECT, related_name="payments", verbose_name=_("Tax Period"))
    payment_date = models.DateField(verbose_name=_("Payment Date"))
    amount_paid  = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Amount Paid"),
    )
    reference = models.CharField(
        max_length=200, blank=True,
        verbose_name=_("Reference"),
        help_text=_("Bank reference, Modelo number, or any external identifier."),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Tax Payment")
        verbose_name_plural = _("Tax Payments")
        ordering = ["-payment_date"]

    def __str__(self):
        return f"{self.amount_paid} on {self.payment_date}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.db.models import Sum
        total = TaxPayment.objects.filter(period=self.period).aggregate(t=Sum("amount_paid"))["t"] or 0
        if total >= self.period.tax_net.amount:
            self.period.status = TaxPeriodStatus.PAID
            self.period.save(update_fields=["status"])


class PaymentCalendar(TaxPeriod):
    """
    Proxy model with no table of its own — exists only so 'Cashflow'
    shows up as its own row in the Finance admin index, next to
    Tax Payments / Tax Periods / Tax Schedule Rules.
    """
    class Meta:
        proxy = True
        verbose_name = _("Cashflow")
        verbose_name_plural = _("Cashflow")


def advance_all_rules():
    """
    - Cleans up misaligned OPEN/OVERDUE periods per rule
    - Generates missing periods up to today for all active rules
    - Marks overdue periods
    """
    from django.utils import timezone
    from dateutil.relativedelta import relativedelta

    today = timezone.now().date()

    for rule in TaxScheduleRule.objects.filter(is_active=True):
        _clean_misaligned_periods(rule)
        rule.generate_up_to_today()

    TaxPeriod.objects.filter(
        due_date__lt=today,
        status__in=[TaxPeriodStatus.OPEN, TaxPeriodStatus.PENDING_PAYMENT],
    ).update(status=TaxPeriodStatus.OVERDUE)


def _clean_misaligned_periods(rule):
    """
    Delete OPEN and OVERDUE periods that don't align with the rule's
    current start_date and frequency. PAID and PENDING_PAYMENT are never touched.
    """
    from dateutil.relativedelta import relativedelta
    from django.utils import timezone

    candidates = list(
        TaxPeriod.objects.filter(
            rule=rule,
            status__in=[TaxPeriodStatus.OPEN, TaxPeriodStatus.OVERDUE],
        )
    )
    if not candidates:
        return

    today = timezone.now().date()
    valid_starts = set()
    start = rule.start_date
    while start <= today:
        end = start + relativedelta(**{rule.period_unit: rule.period_length}) - relativedelta(days=1)
        valid_starts.add(start)
        start = end + relativedelta(days=1)

    stale_ids = [p.pk for p in candidates if p.date_from not in valid_starts]
    if stale_ids:
        TaxPeriod.objects.filter(pk__in=stale_ids).delete()