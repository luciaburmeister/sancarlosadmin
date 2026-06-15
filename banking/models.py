from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from djmoney.models.fields import MoneyField
from company.models import CompanyOwnedModel, BankAccount

User = get_user_model()


class ReconciliationStatus(models.TextChoices):
    UNRECONCILED = "unreconciled", _("Unreconciled")
    PARTIAL = "partial", _("Partial")
    RECONCILED = "reconciled", _("Reconciled")


class ReconciliationMethod(models.TextChoices):
    AUTO = "auto", _("Automatic")
    MANUAL = "manual", _("Manual")


class BankTransaction(CompanyOwnedModel):
    """
    Raw bank statement line imported from Nuek.
    Immutable ground truth — never edited after creation.
    """
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        related_name="transactions",
        verbose_name=_("Bank Account"),
        help_text=_("The company bank account this transaction belongs to.")
    )
    external_id = models.CharField(
        max_length=255,
        unique=True,
        verbose_name=_("External ID"),
        help_text=_("Unique transaction ID assigned by Nuek or the bank. Used to prevent duplicate imports.")
    )
    transaction_date = models.DateField(
        db_index=True,
        verbose_name=_("Transaction Date"),
        help_text=_("The date the bank registered this movement.")
    )
    amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        verbose_name=_("Amount"),
        help_text=_("Positive = money in, negative = money out.")
    )
    currency = models.CharField(
        max_length=3,
        default="EUR",
        verbose_name=_("Currency"),
        help_text=_("Currency code (EUR, USD, etc.)")
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        help_text=_("Raw text description from the bank, e.g. 'TRANSF. ENDESA ENERGIA S.A.'")
    )
    counterparty_name = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Counterparty Name"),
        help_text=_("Name of who sent or received the money, if provided by the bank.")
    )
    counterparty_iban = models.CharField(
        max_length=34,
        blank=True,
        db_index=True,
        verbose_name=_("Counterparty IBAN"),
        help_text=_("IBAN of who sent or received the money. Primary field used for auto-matching.")
    )
    raw_data = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("Raw Data"),
        help_text=_("Full JSON response from Nuek, stored as-is. Used as a safety net for backfilling fields later.")
    )
    reconciliation_status = models.CharField(
        max_length=20,
        choices=ReconciliationStatus.choices,
        default=ReconciliationStatus.UNRECONCILED,
        db_index=True,
        verbose_name=_("Reconciliation Status"),
        help_text=_("Whether this transaction has been matched to a Payment record.")
    )
    imported_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Imported At"),
        help_text=_("Timestamp when this transaction was imported into the system.")
    )

    class Meta:
        verbose_name = _("Bank Transaction")
        verbose_name_plural = _("Bank Transactions")
        ordering = ["-transaction_date", "-imported_at"]
        indexes = [
            models.Index(fields=["bank_account", "transaction_date"]),
            models.Index(fields=["reconciliation_status", "transaction_date"]),
        ]

    def __str__(self):
        return f"{self.transaction_date} | {self.amount} | {self.counterparty_name or self.description[:40]}"


class BankReconciliation(models.Model):
    """
    Created by the auto-matcher immediately after a BankTransaction is imported.
    Starts as an unconfirmed suggestion (is_confirmed=False, payment=None).
    A human reviews it and confirms, at which point a Payment is created and linked.
    """
    bank_transaction = models.OneToOneField(
        BankTransaction,
        on_delete=models.CASCADE,
        related_name="reconciliation",
        verbose_name=_("Bank Transaction"),
        help_text=_("The bank statement line being reconciled.")
    )
    payment = models.OneToOneField(
        "payments.Payment",
        on_delete=models.CASCADE,
        related_name="reconciliation",
        null=True,
        blank=True,
        verbose_name=_("Payment"),
        help_text=_("Null until a human confirms the suggestion. Set when the Payment is created on confirmation.")
    )
    is_confirmed = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_("Is Confirmed"),
        help_text=_("False = auto-matcher suggestion pending human review. True = human confirmed.")
    )
    suggested_allocations = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Suggested Allocations"),
        help_text=_(
            "List of matching combinations found by the auto-matcher. "
            "Each entry: {'allocations': [...], 'confidence': 'exact', 'is_selected': bool}. "
            "The first entry with is_selected=True is the primary suggestion."
        ),
    )
    unmatched_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Unmatched Amount"),
        help_text=_("How much of the transaction amount was not matched to any document. Zero means a full match was found.")
    )
    reconciled_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created At"),
        help_text=_("Timestamp when this reconciliation record was first created by the matcher.")
    )
    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Confirmed At"),
        help_text=_("Timestamp when a human confirmed this reconciliation.")
    )
    reconciled_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reconciliations",
        verbose_name=_("Confirmed By"),
        help_text=_("The user who confirmed this match. Null until confirmed.")
    )
    method = models.CharField(
        max_length=10,
        choices=ReconciliationMethod.choices,
        default=ReconciliationMethod.AUTO,
        verbose_name=_("Method"),
        help_text=_("Auto = created by the matcher. Manual = human confirmed or created from scratch.")
    )
    notes = models.TextField(
        blank=True,
        verbose_name=_("Notes"),
        help_text=_("Optional notes about why this match was made.")
    )

    class Meta:
        verbose_name = _("Bank Reconciliation")
        verbose_name_plural = _("Bank Reconciliations")
        ordering = ["-reconciled_at"]

    def __str__(self):
        status = "confirmed" if self.is_confirmed else "pending review"
        return f"{self.bank_transaction} ↔ {self.payment or 'unconfirmed'} ({status})"