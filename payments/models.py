from django.db import models, transaction
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from django.db.models import Sum
from company.models import CompanyOwnedModel, BankAccount


class PaymentDirection(models.TextChoices):
    INBOUND = "inbound", _("Inbound")
    OUTBOUND = "outbound", _("Outbound")


class PaymentStatus(models.TextChoices):
    DRAFT = "draft", _("Draft") #payment has been created but not confirmed yet
    CONFIRMED = "confirmed", _("Confirmed")
    RECONCILED = "reconciled", _("Reconciled") #payment has been fully matched/allocated
    CANCELLED = "cancelled", _("Cancelled")


class PaymentMethod(models.TextChoices):
    TRANSFER = "transfer", _("Bank Transfer")
    CASH = "cash", _("Cash")
    CARD = "card", _("Credit/Debit Card")
    CHEQUE = "cheque", _("Cheque")
    SEPA_DIRECT_DEBIT = "sepa_direct_debit", _("SEPA - Direct debit")
    CONFIRMING = "confirming", _("Confirming")

class PaymentCreatedBy(models.TextChoices):
    MANUAL = "manual", _("Manual")
    AUTO = "auto", _("Automatic")

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
    bank_account = models.ForeignKey(
    BankAccount,
    on_delete=models.PROTECT,
    null=True,
    blank=True,
    related_name="payments",
    verbose_name=_("Bank Account"),
    help_text=_("The company bank account this payment went through. Required for bank transfers.")
    )
    created_by = models.CharField(
        max_length=10,
        choices=PaymentCreatedBy.choices,
        default=PaymentCreatedBy.MANUAL,
        verbose_name=_("Created By"),
        help_text=_("Whether this payment was created manually by a user or automatically from a bank transaction.")
    )
    client = models.ForeignKey(
        "sales.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="payments",
        verbose_name=_("Client"),
        help_text=_(
            "The client this money came from (or went back to). Filled in "
            "automatically the first time the payment is allocated to one of "
            "their sales invoices; set it by hand for a payment on account "
            "that has no invoice yet."
        ),
    )

    class Meta:
        verbose_name = _("Payment")
        verbose_name_plural = _("Payments")
        ordering = ["-payment_date", "-id"]

    def __str__(self):
        return f"{self.get_direction_display()} {self.amount} — {self.payment_date}"

    def clean(self):
        if self.method == PaymentMethod.TRANSFER and not self.bank_account_id:
            raise ValidationError({
                "bank_account": _("A bank account is required for bank transfers.")
            })

    def save(self, *args, **kwargs):
        # A brand-new payment has no allocations yet, so everything is
        # pending. Callers used to have to pass amount_pending_allocation
        # themselves; the ones that didn't left a payment on account reading
        # as fully allocated (0 - 0), which hid it from the client's credit.
        if self._state.adding:
            self.amount_pending_allocation = Money(self.amount.amount, self.amount_currency)
        super().save(*args, **kwargs)

    def sync_client_from_document(self, content_type, object_id):
        """
        Take the client off a sales invoice this payment was just allocated
        to, when nobody has set one yet. Never overrides an explicit client.
        """
        if self.client_id:
            return
        client_id = _client_id_for_document(content_type, object_id)
        if client_id:
            self.client_id = client_id
            self.save(update_fields=["client"])

    def recalculate_pending_allocation(self):
        """
        Recompute amount_pending_allocation from allocations and save.
        Always call this inside a transaction.atomic() block.
        """
        allocated = (
            self.allocations.exclude(is_mirror=True)
            .aggregate(total=Sum("amount_applied"))["total"] or 0
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
    is_mirror = models.BooleanField(
        default=False,
        verbose_name=_("Mirrored"),
        help_text=_(
            "True for allocations created automatically by sync_linked_payable() "
            "to reflect that this same payment also settles a linked document "
            "(e.g. a FixedCostInstallment and its matched_invoice). Doesn't "
            "represent additional real money - excluded from the payment's own "
            "amount_pending_allocation so it isn't counted twice."
        ),
    )

    class Meta:
        verbose_name = _("Payment Allocation")
        verbose_name_plural = _("Payment Allocations")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        return f"{self.amount_applied} allocated to {self.related_object}"

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            self.payment.recalculate_pending_allocation()
            self.payment.sync_client_from_document(self.content_type, self.object_id)
            _recalculate_invoice_payment_status(self.content_type, self.object_id)


@receiver(post_delete, sender=PaymentAllocation)
def _recalculate_on_allocation_delete(sender, instance, **kwargs):
    """
    Keeps the payment's pending allocation and the document's payment_status
    in sync also on cascade deletes (e.g. deleting the Payment itself), which
    bypass PaymentAllocation.delete() and previously left invoices stuck as
    "paid" after their only payment was removed.
    """
    payment = Payment.objects.filter(pk=instance.payment_id).first()
    if payment:
        payment.recalculate_pending_allocation()
    _recalculate_invoice_payment_status(instance.content_type, instance.object_id)


@receiver(pre_delete, sender="invoices.Invoice")
def _cleanup_fixed_cost_match_before_invoice_delete(sender, instance, **kwargs):
    """
    FixedCostInstallment.matched_invoice uses on_delete=SET_NULL, which Django
    applies as a direct DB update when the Invoice is deleted - it never calls
    FixedCostInstallment.save(), so the save()-level cleanup that normally
    removes a stale mirror allocation when matched_invoice changes never
    runs. Without this, a deleted invoice leaves its installment's mirror
    PaymentAllocation (and therefore its is_paid/payment_status) orphaned,
    pointing at nothing. Runs pre_delete, before SET_NULL clears the FK, so
    the lookup below still finds the installment(s).
    """
    from django.apps import apps

    FixedCostInstallment = apps.get_model("expenses", "FixedCostInstallment")
    ct = ContentType.objects.get_for_model(FixedCostInstallment)
    installment_ids = FixedCostInstallment.objects.filter(
        matched_invoice=instance,
    ).values_list("pk", flat=True)
    PaymentAllocation.objects.filter(
        content_type=ct, object_id__in=list(installment_ids), is_mirror=True,
    ).delete()


def _client_id_for_document(content_type, object_id):
    """The client behind an allocated document, or None if it has none (sales invoices only today)."""
    from django.apps import apps

    SalesInvoice = apps.get_model("invoices", "SalesInvoice")
    if content_type.model_class() is not SalesInvoice:
        return None
    return (
        SalesInvoice.objects.filter(pk=object_id)
        .values_list("client_id", flat=True)
        .first()
    )


def _recalculate_invoice_payment_status(content_type, object_id):
    """
    If the allocated object is a SalesInvoice, Invoice, LoanInstallment,
    ExpenseClaim, or FixedCostInstallment, trigger its recalculation method.
    Uses apps.get_model to avoid circular imports.
    """
    from django.apps import apps

    SalesInvoice = apps.get_model("invoices", "SalesInvoice")
    Invoice = apps.get_model("invoices", "Invoice")
    LoanInstallment = apps.get_model("loans", "LoanInstallment")
    ExpenseClaim = apps.get_model("expenses", "ExpenseClaim")
    FixedCostInstallment = apps.get_model("expenses", "FixedCostInstallment")
    model_class = content_type.model_class()

    if model_class is SalesInvoice:
        from configurator.commission.services import CommissionReleaseService

        try:
            invoice = SalesInvoice.objects.get(pk=object_id)
            invoice.recalculate_payment_status()
            CommissionReleaseService.sync_for_invoice(invoice)
        except SalesInvoice.DoesNotExist:
            pass
    elif model_class is Invoice:
        try:
            invoice = Invoice.objects.get(pk=object_id)
            invoice.recalculate_payment_status()
        except Invoice.DoesNotExist:
            pass
    elif model_class is LoanInstallment:
        try:
            installment = LoanInstallment.objects.get(pk=object_id)
            installment.recalculate_payment_status()
        except LoanInstallment.DoesNotExist:
            pass
    elif model_class is ExpenseClaim:
        try:
            claim = ExpenseClaim.objects.get(pk=object_id)
            claim.recalculate_reimbursement()
        except ExpenseClaim.DoesNotExist:
            pass
    elif model_class is FixedCostInstallment:
        try:
            installment = FixedCostInstallment.objects.get(pk=object_id)
            installment.recalculate_payment_status()
        except FixedCostInstallment.DoesNotExist:
            pass


def sync_linked_payable(source, target):
    """
    Mirror source's confirmed PaymentAllocations onto target, up to target's
    remaining balance.

    Used when two Payable documents have been confirmed to represent the same
    real-world expense (today: a FixedCostInstallment and its matched_invoice)
    - once linked, they're meant to behave as one: if either side gets
    reconciled against a real bank payment, the other should inherit it
    instead of sitting there looking unpaid.

    Idempotent - skips any Payment that target already has an allocation
    from, so calling this from both sides (as the recalculate_payment_status
    of each linked model does) converges instead of looping or double-booking
    the same payment onto the same document twice.
    """
    remaining = target.amount_remaining_decimal
    if remaining <= 0:
        return

    target_ct = ContentType.objects.get_for_model(target)
    source_ct = ContentType.objects.get_for_model(source)

    already_applied_payment_ids = set(
        PaymentAllocation.objects.filter(
            content_type=target_ct, object_id=target.pk,
        ).values_list("payment_id", flat=True)
    )

    source_allocations = (
        PaymentAllocation.objects.filter(content_type=source_ct, object_id=source.pk)
        .exclude(is_mirror=True)
        .exclude(payment_id__in=already_applied_payment_ids)
        .select_related("payment")
    )

    for alloc in source_allocations:
        if remaining <= 0:
            break
        amount_to_apply = min(alloc.amount_applied.amount, remaining)
        PaymentAllocation.objects.create(
            payment=alloc.payment,
            content_type=target_ct,
            object_id=target.pk,
            amount_applied=amount_to_apply,
            is_mirror=True,
        )
        remaining -= amount_to_apply


def desync_linked_payable(source, target):
    """
    Remove any mirror PaymentAllocation on target that was sourced from a
    Payment source no longer has a real (non-mirror) allocation for - the
    reverse of sync_linked_payable(), needed when a real allocation on
    source gets deleted or reduced (e.g. a bank reconciliation gets undone),
    so the mirror doesn't outlive the real money it was reflecting.

    Deleting a stale mirror here triggers PaymentAllocation's own post_delete
    signal, which recalculates target's payment_status - that's what lets a
    FixedCostInstallment fall back to unpaid on its own when its matched
    invoice's reconciliation gets undone.
    """
    source_ct = ContentType.objects.get_for_model(source)
    target_ct = ContentType.objects.get_for_model(target)

    source_payment_ids = set(
        PaymentAllocation.objects.filter(content_type=source_ct, object_id=source.pk)
        .exclude(is_mirror=True)
        .values_list("payment_id", flat=True)
    )
    PaymentAllocation.objects.filter(
        content_type=target_ct, object_id=target.pk, is_mirror=True,
    ).exclude(payment_id__in=source_payment_ids).delete()
