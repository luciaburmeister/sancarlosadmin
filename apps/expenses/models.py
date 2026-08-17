from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.validators import FileExtensionValidator
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django_countries.fields import CountryField
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from company.models import CompanyOwnedModel
from payments.mixins import Payable
from decimal import Decimal
import requests
import os
import uuid

User = get_user_model()


# ─────────────────────────────────────────────
# EXCHANGE RATE
# ─────────────────────────────────────────────

class ExchangeRateSource(models.TextChoices):
    FRANKFURTER = 'frankfurter', _('Frankfurter API')
    FAWAZ_AHMED = 'fawaz_ahmed', _('Fawaz Ahmed Currency API')
    MANUAL = 'manual', _('Manual Override')


class ExchangeRate(models.Model):
    rate_date = models.DateField(
        db_index=True,
        verbose_name=_("Rate Date"),
        help_text="The date this exchange rate applies to"
    )
    from_currency = models.CharField(
        max_length=3,
        verbose_name=_("From Currency"),
        help_text="Currency being converted from, e.g. USD"
    )
    to_currency = models.CharField(
        max_length=3,
        verbose_name=_("To Currency"),
        help_text="Currency being converted to, e.g. EUR"
    )
    rate = models.DecimalField(
        max_digits=12,
        decimal_places=6,
        verbose_name=_("Exchange Rate"),
        help_text="How many units of to_currency equal 1 unit of from_currency"
    )
    source = models.CharField(
        max_length=20,
        choices=ExchangeRateSource.choices,
        default=ExchangeRateSource.FRANKFURTER,
        verbose_name=_("Source")
    )
    manually_overridden = models.BooleanField(
        default=False,
        verbose_name=_("Manually Overridden"),
        help_text="True if a user manually set this rate instead of using the API"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Exchange Rate")
        verbose_name_plural = _("Exchange Rates")
        ordering = ['-rate_date']
        unique_together = ['rate_date', 'from_currency', 'to_currency', 'manually_overridden']

    def __str__(self):
        return f"{self.from_currency} → {self.to_currency} on {self.rate_date} ({self.rate})"

    def convert(self, amount: Decimal) -> Decimal:
        return (Decimal(str(amount)) * self.rate).quantize(Decimal("0.01"))

    @classmethod
    def get_or_fetch(cls, from_currency: str, to_currency: str, date) -> 'ExchangeRate':
        existing = cls.objects.filter(
            rate_date=date,
            from_currency=from_currency,
            to_currency=to_currency,
            manually_overridden=False
        ).first()

        if existing:
            return existing

        # Try Frankfurter first (ECB data, ~30 major currencies)
        frankfurter_response = requests.get(
            f"https://api.frankfurter.app/{date}",
            params={"from": from_currency, "to": to_currency},
        )
        if frankfurter_response.status_code == 200:
            data = frankfurter_response.json()
            if "rates" in data and to_currency in data["rates"]:
                rate_value = Decimal(str(data["rates"][to_currency]))
                return cls.objects.create(
                    rate_date=date,
                    from_currency=from_currency,
                    to_currency=to_currency,
                    rate=rate_value,
                    source=ExchangeRateSource.FRANKFURTER,
                    manually_overridden=False,
                )

        # Fallback: Fawaz Ahmed open currency dataset (supports 150+ currencies with history)
        fallback_response = requests.get(
            f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/{from_currency.lower()}.json"
        )
        fallback_response.raise_for_status()
        data = fallback_response.json()
        rate_value = data.get(from_currency.lower(), {}).get(to_currency.lower())
        if rate_value is None:
            raise ValueError(
                f"No exchange rate found for {from_currency} → {to_currency} on {date}"
            )

        return cls.objects.create(
            rate_date=date,
            from_currency=from_currency,
            to_currency=to_currency,
            rate=Decimal(str(rate_value)),
            source=ExchangeRateSource.FAWAZ_AHMED,
            manually_overridden=False,
        )


# ─────────────────────────────────────────────
# EXPENSE CLAIM (defined before ExpenseTicket
# so ExpenseTicket can reference it)
# ─────────────────────────────────────────────

class ExpenseClaimStatus(models.TextChoices):
    DRAFT = 'draft', _('Draft')
    SUBMITTED = 'submitted', _('Submitted')
    APPROVED = 'approved', _('Approved')
    REIMBURSED = 'reimbursed', _('Reimbursed')
    REJECTED = 'rejected', _('Rejected')


VALID_TRANSITIONS = {
    ExpenseClaimStatus.DRAFT: [ExpenseClaimStatus.SUBMITTED],
    ExpenseClaimStatus.SUBMITTED: [ExpenseClaimStatus.APPROVED, ExpenseClaimStatus.REJECTED],
    ExpenseClaimStatus.APPROVED: [ExpenseClaimStatus.REIMBURSED],
    ExpenseClaimStatus.REIMBURSED: [],
    # A rejected claim goes back to the submitter as a draft so they can fix
    # it and resubmit - otherwise it would be a permanent dead end.
    ExpenseClaimStatus.REJECTED: [ExpenseClaimStatus.DRAFT],
}


class ExpenseClaim(CompanyOwnedModel, Payable):

    submitted_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='expense_claims',
        verbose_name=_("Submitted By")
    )
    status = models.CharField(
        max_length=20,
        choices=ExpenseClaimStatus.choices,
        default=ExpenseClaimStatus.DRAFT,
        verbose_name=_("Status")
    )
    total_claimed = MoneyField(
        max_digits=10,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Total Claimed"),
        help_text="Auto-calculated from attached tickets"
    )
    total_reimbursed = MoneyField(
        max_digits=10,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Total Reimbursed"),
        help_text="Amount actually reimbursed so far, cannot exceed total_claimed"
    )
    rejection_reason = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Rejection Reason"),
        help_text="Required when rejecting a claim"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Expense Claim")
        verbose_name_plural = _("Expense Claims")
        ordering = ['-created_at']
        permissions = [
            ("approve_expenseclaim", "Can approve or reject expense claims"),
        ]

    def __str__(self):
        return f"Claim #{self.pk} – {self.submitted_by} ({self.status})"

    @property
    def amount_remaining(self):
        return self.total_claimed - self.total_reimbursed

    @property
    def counterparty_key(self) -> tuple:
        """Reimbursement goes to the submitting employee, not a provider/client."""
        return ("employee", self.submitted_by_id)

    def recalculate_reimbursement(self):
        """
        Recompute total_reimbursed from PaymentAllocation entries pointing to
        this claim, and auto-transition to REIMBURSED once fully covered.
        Mirrors Invoice.recalculate_payment_status(). Always call inside a
        transaction.atomic() block.
        """
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(self)
        total_applied = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or Decimal("0.00")
        )
        total_applied = min(total_applied, self.total_claimed.amount)

        self.total_reimbursed = Money(total_applied, self.total_claimed_currency)
        if total_applied >= self.total_claimed.amount and self.status == ExpenseClaimStatus.APPROVED:
            self.status = ExpenseClaimStatus.REIMBURSED
        self.save(update_fields=["total_reimbursed", "status"])

    def recalculate_total(self, save=True):
        tickets = self.tickets.all()
        total = Decimal("0.00")
        for ticket in tickets:
            total += ticket.get_effective_amount()
        self.total_claimed = total
        if save:
            # Pass update_fields to skip full_clean() — avoids false validation errors
            self.save(update_fields=['total_claimed'])
        return self.total_claimed

    def clean(self):
        if self.total_reimbursed and self.total_claimed:
            if self.total_reimbursed.amount > self.total_claimed.amount:
                raise ValidationError({
                    'total_reimbursed': _(
                        "Reimbursed amount (%(reimbursed)s) cannot exceed "
                        "total claimed (%(claimed)s)."
                    ) % {
                        'reimbursed': self.total_reimbursed,
                        'claimed': self.total_claimed
                    }
                })
        if self.status == ExpenseClaimStatus.REJECTED and not self.rejection_reason:
            raise ValidationError({
                'rejection_reason': _("A rejection reason is required when rejecting a claim.")
            })

    def transition_to(self, new_status: str):
        allowed = VALID_TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValidationError(
                _("Cannot transition from '%(current)s' to '%(new)s'.") % {
                    'current': self.get_status_display(),
                    'new': dict(ExpenseClaimStatus.choices).get(new_status, new_status)
                }
            )
        self.status = new_status
        self.save(update_fields=['status', 'updated_at'])

    def save(self, *args, **kwargs):
        # Only run full_clean() on full saves, not partial update_fields saves
        if not kwargs.get('update_fields'):
            self.full_clean()
        super().save(*args, **kwargs)


# ─────────────────────────────────────────────
# EXPENSE TICKET
# ─────────────────────────────────────────────

def expense_ticket_upload_path(instance, filename):
    ext = os.path.splitext(filename)[1] or '.jpg'
    uuid_name = f"{uuid.uuid4().hex}{ext}"
    company_id = instance.company_id or 'unknown'
    user_id = instance.submitted_by_id or 'unknown'
    return f"expense_tickets/company_{company_id}/user_{user_id}/{uuid_name}"


class ExpenseTicket(CompanyOwnedModel):

    submitted_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='expense_tickets',
        verbose_name=_("Submitted By")
    )
    claim = models.ForeignKey(
        ExpenseClaim,                  # direct reference now, no string needed
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='tickets',
        verbose_name=_("Expense Claim")
    )
    photo = models.FileField(
        upload_to=expense_ticket_upload_path,
        validators=[
            FileExtensionValidator(allowed_extensions=['pdf', 'png', 'jpg', 'jpeg'])
        ],
        verbose_name=_("Photo"),
        help_text="Upload a photo or scan of the receipt (PDF, PNG, JPG)"
    )
    date_of_purchase = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Date of Purchase"),
        help_text="Date printed on the ticket, filled by OCR"
    )
    ticket_number = models.CharField(
        max_length=100,
        blank=True,
        default='',
        verbose_name=_("Ticket Number"),
        help_text="Transaction/ticket/receipt number printed on the ticket, filled by OCR"
    )
    invoice = models.ForeignKey(
        'invoices.Invoice',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expense_tickets',
        verbose_name=_("Invoice"),
        help_text="Optional formal Invoice for this expense — attach an existing one, "
                   "or generate one from this ticket's data."
    )

    # --- Vendor fields ---
    provider = models.ForeignKey(
        'materials.Provider',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='expense_tickets',
        verbose_name=_("Provider"),
        help_text="Linked Provider record, auto-matched by tax ID during OCR (or set manually)."
    )
    vendor_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        verbose_name=_("Vendor Name")
    )
    vendor_tax_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Vendor Tax ID"),
        help_text="CIF, NIF, VAT number, or equivalent"
    )
    vendor_address = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Vendor Address")
    )
    vendor_city = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Vendor City")
    )
    vendor_country = CountryField(
        blank=True,
        null=True,
        verbose_name=_("Vendor Country")
    )

    # --- Amount fields ---
    original_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        default_currency='EUR',
        verbose_name=_("Original Amount"),
        help_text="Amount exactly as printed on the ticket"
    )
    exchange_rate = models.ForeignKey(
        ExchangeRate,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='expense_tickets',
        verbose_name=_("Exchange Rate"),
        help_text="Null if ticket currency matches company base currency"
    )
    converted_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        default_currency='EUR',
        verbose_name=_("Converted Amount"),
        help_text="Amount in company base currency. Null if no conversion needed."
    )

    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        blank=True,
        help_text="SHA-256 hash for duplicate receipt detection"
    )

    # --- OCR fields ---
    ocr_processed = models.BooleanField(default=False)
    ocr_processing = models.BooleanField(default=False)
    ocr_error = models.TextField(blank=True, null=True)
    ocr_raw_response = models.JSONField(blank=True, null=True)
    needs_manual_review = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Expense Ticket")
        verbose_name_plural = _("Expense Tickets")
        ordering = ['-date_of_purchase', '-created_at']

    def __str__(self):
        return f"{self.vendor_name} – {self.original_amount} ({self.submitted_by})"

    def get_effective_amount(self) -> Decimal:
        if self.converted_amount is not None:
            return self.converted_amount.amount
        if self.original_amount is not None:
            return self.original_amount.amount
        return Decimal("0.00")

    def apply_exchange_rate(self, company_currency: str, save: bool = True):
        if not self.original_amount or not self.date_of_purchase:
            return
        ticket_currency = self.original_amount_currency
        if ticket_currency == company_currency:
            self.exchange_rate = None
            self.converted_amount = None
        else:
            rate = ExchangeRate.get_or_fetch(
                from_currency=ticket_currency,
                to_currency=company_currency,
                date=self.date_of_purchase
            )
            self.exchange_rate = rate
            self.converted_amount = rate.convert(self.original_amount.amount)
        if save:
            self.save(update_fields=['exchange_rate', 'converted_amount'])

    def save(self, *args, **kwargs):
        # Lock ticket if its claim has moved past draft
        if self.claim_id and not kwargs.get('update_fields'):
            if self.claim.status != ExpenseClaimStatus.DRAFT:
                raise ValidationError(
                    _("Cannot edit a ticket that belongs to a submitted claim.")
                )
        super().save(*args, **kwargs)