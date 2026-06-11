from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from djmoney.models.fields import MoneyField
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel


class PeriodUnit(models.TextChoices):
    DAYS   = "days",   _("Días")
    MONTHS = "months", _("Meses")
    YEARS  = "years",  _("Años")


class TaxPeriodStatus(models.TextChoices):
    OPEN            = "open",            _("Abierto")
    PENDING_PAYMENT = "pending_payment", _("Pendiente de pago")
    PAID            = "paid",            _("Pagado")
    OVERDUE         = "overdue",         _("Vencido")


# ─────────────────────────────────────────────────────────────────────────────
# TaxScheduleRule
# ─────────────────────────────────────────────────────────────────────────────

class TaxScheduleRule(Timestampable, CompanyOwnedModel):
    name = models.CharField(
        max_length=150,
        verbose_name=_("Nombre de la regla"),
        help_text=_("Ej: 'IVA Trimestral', 'IRPF Mensual'"),
    )
    tax_type = models.ForeignKey(
        "invoices.TaxType",
        on_delete=models.PROTECT,
        related_name="schedule_rules",
        verbose_name=_("Tipo de impuesto"),
        help_text=_("Las facturas con este tipo de impuesto se acumularán automáticamente en los períodos."),
    )
    start_date = models.DateField(
        verbose_name=_("Fecha de inicio"),
        help_text=_("Fecha en la que empieza el primer período de acumulación."),
        default="2026-01-01",
    )
    period_length = models.PositiveIntegerField(
        verbose_name=_("Duración del período"),
        help_text=_("Número de unidades que dura cada período. Ej: 3 + Meses = trimestral, 1 + Meses = mensual."),
        validators=[MinValueValidator(1)],
    )
    period_unit = models.CharField(
        max_length=10,
        choices=PeriodUnit.choices,
        default=PeriodUnit.MONTHS,
        verbose_name=_("Unidad del período"),
    )
    payment_day = models.PositiveIntegerField(
        verbose_name=_("Día de pago"),
        help_text=_("Día del mes en que vence el pago. Ej: 20 → vence el día 20."),
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    meses_hasta_vencimiento = models.PositiveIntegerField(
        default=1,
        verbose_name=_("Meses hasta el vencimiento"),
        help_text=_(
            "Meses desde el cierre del período hasta el vencimiento. "
            "Ej: IVA trimestral — el período cierra el 31 de marzo, "
            "con 1 mes hasta vencimiento el pago vence el 20 de abril."
        ),
        validators=[MinValueValidator(1)],
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Activa"),
        help_text=_("Las reglas inactivas no generan nuevos períodos."),
    )

    class Meta:
        verbose_name = _("Regla fiscal")
        verbose_name_plural = _("Reglas fiscales")
        ordering = ["company", "name"]

    def __str__(self):
        return f"{self.name} ({self.company})"

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


# ─────────────────────────────────────────────────────────────────────────────
# TaxPeriod
# ─────────────────────────────────────────────────────────────────────────────

class TaxPeriod(Timestampable, CompanyOwnedModel):
    rule      = models.ForeignKey(TaxScheduleRule, on_delete=models.PROTECT, related_name="periods", verbose_name=_("Regla fiscal"))
    date_from = models.DateField(verbose_name=_("Inicio del período"))
    date_to   = models.DateField(verbose_name=_("Fin del período"))
    due_date  = models.DateField(verbose_name=_("Fecha de vencimiento"))
    status    = models.CharField(
        max_length=20,
        choices=TaxPeriodStatus.choices,
        default=TaxPeriodStatus.OPEN,
        verbose_name=_("Estado"),
    )

    tax_collected = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Impuesto repercutido"),
        help_text=_("Total acumulado de impuesto de facturas de venta en este período."),
    )
    tax_paid = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Impuesto soportado"),
        help_text=_("Total acumulado de impuesto de facturas de compra en este período."),
    )
    tax_net = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Impuesto a ingresar"),
        help_text=_("Impuesto repercutido − Impuesto soportado. Importe a pagar a Hacienda."),
    )
    accumulated_credit = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Saldo a compensar"),
        help_text=_(
            "IVA negativo acumulado de períodos anteriores del mismo año natural. "
            "Hacienda lo descuenta de futuros resultados positivos."
        ),
    )
    net_after_credit = MoneyField(
        max_digits=14, decimal_places=2, default=0, default_currency="EUR",
        verbose_name=_("Neto a pagar"),
        help_text=_(
            "Impuesto a ingresar una vez aplicado el saldo a compensar acumulado. "
            "Si es negativo, el exceso se arrastra al siguiente período del año."
        ),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notas"))

    class Meta:
        verbose_name = _("Período fiscal")
        verbose_name_plural = _("Períodos fiscales")
        ordering = ["-date_from"]
        unique_together = [["rule", "date_from"]]

    def __str__(self):
        return f"{self.rule.tax_type} | {self.date_from} → {self.date_to}"

    def compute(self, save: bool = True):
        """
        Aggregate AppliedTax amounts from invoices within this period's
        date range, filtered by company and the rule's tax_type.
        Sums the numeric column directly to avoid djmoney Money aggregation issues.
        """
        from django.contrib.contenttypes.models import ContentType
        from invoices.models import AppliedTax, SalesInvoice, Invoice
        from django.db.models import Sum
        from decimal import Decimal

        tax_type = self.rule.tax_type

        # IVA repercutido — facturas de venta
        sales_ct = ContentType.objects.get_for_model(SalesInvoice)
        sales_ids = list(SalesInvoice.objects.filter(
            company=self.company,
            date_issued__range=(self.date_from, self.date_to),
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

        # IVA soportado — facturas de compra
        purchase_ct = ContentType.objects.get_for_model(Invoice)
        purchase_ids = list(Invoice.objects.filter(
            company=self.company,
            date_issued__range=(self.date_from, self.date_to),
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

        # ── Saldo a compensar acumulado ────────────────────────────────────
        # Accumulate all negative tax_net from ALL prior periods of the same
        # rule, with no year boundary — credit carries forward indefinitely.
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
        net_after = net_val - accumulated  # positive = pay to Hacienda, negative = carry forward
        self.net_after_credit = net_after

        if save:
            self.save(update_fields=[
                "tax_collected", "tax_collected_currency",
                "tax_paid", "tax_paid_currency",
                "tax_net", "tax_net_currency",
                "accumulated_credit", "accumulated_credit_currency",
                "net_after_credit", "net_after_credit_currency",
            ])


# ─────────────────────────────────────────────────────────────────────────────
# TaxPayment
# ─────────────────────────────────────────────────────────────────────────────

class TaxPayment(Timestampable, CompanyOwnedModel):
    period       = models.ForeignKey(TaxPeriod, on_delete=models.PROTECT, related_name="payments", verbose_name=_("Período fiscal"))
    payment_date = models.DateField(verbose_name=_("Fecha de pago"))
    amount_paid  = MoneyField(
        max_digits=14, decimal_places=2, default_currency="EUR",
        verbose_name=_("Importe pagado"),
    )
    reference = models.CharField(
        max_length=200, blank=True,
        verbose_name=_("Referencia"),
        help_text=_("Referencia bancaria, número de Modelo, o cualquier identificador externo."),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notas"))

    class Meta:
        verbose_name = _("Pago fiscal")
        verbose_name_plural = _("Pagos fiscales")
        ordering = ["-payment_date"]

    def __str__(self):
        return f"{self.amount_paid} el {self.payment_date}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Auto-marcar período como pagado si los pagos cubren el neto
        from django.db.models import Sum
        total = TaxPayment.objects.filter(period=self.period).aggregate(t=Sum("amount_paid"))["t"] or 0
        if total >= self.period.tax_net.amount:
            self.period.status = TaxPeriodStatus.PAID
            self.period.save(update_fields=["status"])
