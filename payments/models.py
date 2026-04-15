from django.db import models, transaction
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from django.db.models import Sum

from company.models import CompanyOwnedModel


class PaymentDirection(models.TextChoices):
    INBOUND = "inbound", _("Inbound")
    OUTBOUND = "outbound", _("Outbound")


class PaymentStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    CONFIRMED = "confirmed", _("Confirmed")
    RECONCILED = "reconciled", _("Reconciled")
    CANCELLED = "cancelled", _("Cancelled")


class PaymentMethod(models.TextChoices):
    TRANSFER = "transfer", _("Bank Transfer")
    CASH = "cash", _("Cash")
    CARD = "card", _("Credit/Debit Card")
    CHEQUE = "cheque", _("Cheque")


class Payment(CompanyOwnedModel):
    payment_date = models.DateField(verbose_name=_("Payment Date"))
    amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Amount"),
    )
    direction = models.CharField(
        max_length=10,
        choices=PaymentDirection.choices,
        verbose_name=_("Direction"),
    )
    method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        verbose_name=_("Method"),
    )
    status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.DRAFT,
        verbose_name=_("Status"),
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Reference"),
    )
    notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Notes"),
    )
    amount_pending_allocation = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Amount Pending Allocation"),
        help_text=_(
            "Stored field. Updated inside a transaction on every allocation change."
        ),
    )

    class Meta:
        verbose_name = _("Payment")
        verbose_name_plural = _("Payments")
        ordering = ["-payment_date", "-id"]

    def __str__(self):
        return f"{self.get_direction_display()} {self.amount} — {self.payment_date}"

    def recalculate_pending_allocation(self):
        """
        Recompute amount_pending_allocation from allocations and save.
        Always call this inside a transaction.atomic() block.
        """
        allocated = (
            self.allocations.aggregate(total=Sum("amount_applied"))["total"] or 0
        )
        self.amount_pending_allocation = Money(
            self.amount.amount - allocated,
            self.amount_currency,
        )
        self.save(
            update_fields=[
                "amount_pending_allocation",
                "amount_pending_allocation_currency",
            ]
        )


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name=_("Payment"),
    )
    amount_applied = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Amount Applied"),
    )
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        verbose_name=_("Document Type"),
    )
    object_id = models.PositiveIntegerField(verbose_name=_("Document ID"))
    related_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        verbose_name = _("Payment Allocation")
        verbose_name_plural = _("Payment Allocations")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.amount_applied} → {self.invoice}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.payment.recalculate_pending_allocation()

    def delete(self, *args, **kwargs):
        payment = self.payment
        with transaction.atomic():
            super().delete(*args, **kwargs)
            payment.recalculate_pending_allocation()