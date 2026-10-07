import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django_countries.fields import CountryField
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel
from finance.models import PeriodUnit
from invoices.models import InvoicePaymentStatus
from payments.mixins import Payable, PaidVia
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
    payment_status = models.CharField(
        max_length=20,
        choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
        verbose_name=_("Payment Status"),
        help_text="Auto-calculated from the payments allocated to this claim"
    )
    rejection_reason = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Rejection Reason"),
        help_text="Required when rejecting a claim"
    )
    notes = models.TextField(
        blank=True,
        default='',
        verbose_name=_("Notes"),
        help_text="Free-text notes about this claim"
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
        self.payment_status = self._resolve_payment_status(total_applied)
        if total_applied >= self.total_claimed.amount and self.status == ExpenseClaimStatus.APPROVED:
            self.status = ExpenseClaimStatus.REIMBURSED
        self.save(update_fields=["total_reimbursed", "payment_status", "status"])

    def _resolve_payment_status(self, total_applied: Decimal) -> str:
        """Mirrors Invoice.recalculate_payment_status() so both read the same."""
        claimed = self.total_claimed.amount if self.total_claimed else Decimal("0.00")
        if total_applied <= 0:
            return InvoicePaymentStatus.UNPAID
        if claimed and total_applied >= claimed:
            return InvoicePaymentStatus.PAID
        return InvoicePaymentStatus.PARTIAL

    def recalculate_total(self, save=True):
        tickets = self.tickets.all()
        total = Decimal("0.00")
        for ticket in tickets:
            total += ticket.get_effective_amount()
        self.total_claimed = total
        if save:
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
        ExpenseClaim,                
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

    notes = models.TextField(
        blank=True,
        default='',
        verbose_name=_("Notes"),
        help_text="Free-text notes about this receipt"
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



class FixedCostSchedule(Timestampable, CompanyOwnedModel):
    provider = models.ForeignKey(
        'materials.Provider',
        on_delete=models.PROTECT,
        related_name='fixed_cost_schedules',
        verbose_name=_("Provider"),
    )
    concept = models.CharField(
        max_length=200,
        verbose_name=_("Concept"),
        help_text=_("E.g. 'BMW Renting', 'Office Rent'"),
    )
    amount = MoneyField(
        max_digits=14, decimal_places=2, default_currency='EUR',
        verbose_name=_("Amount"),
    )
    start_date = models.DateField(verbose_name=_("Start Date"))
    period_length = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        verbose_name=_("Period Length"),
        help_text=_("Number of units per period. E.g. 1 + Months = monthly."),
    )
    period_unit = models.CharField(
        max_length=10,
        choices=PeriodUnit.choices,
        default=PeriodUnit.MONTHS,
        verbose_name=_("Period Unit"),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Inactive schedules will not generate new installments."),
    )

    class Meta:
        verbose_name = _("Fixed Cost Schedule")
        verbose_name_plural = _("Fixed Cost Schedules")
        ordering = ["company", "concept"]

    def __str__(self):
        return self.concept

    REGEN_FIELDS = {"start_date", "period_length", "period_unit", "amount"}

    # How far into the future an installment is generated before it's due -
    # e.g. 30 means "create it once it's within 30 days of being due", not
    # the whole future at once (FixedCostSchedule has no end date).
    HORIZON_DAYS = 30

    def save(self, *args, **kwargs):
        is_new = self.pk is None

        changed = set()
        if not is_new:
            try:
                old = FixedCostSchedule.objects.get(pk=self.pk)
                changed = {f for f in self.REGEN_FIELDS if getattr(old, f) != getattr(self, f)}
            except FixedCostSchedule.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        if not self.is_active:
            return

        if is_new or (changed & self.REGEN_FIELDS):

            FixedCostInstallment.objects.filter(
                schedule=self,
                payment_status=InvoicePaymentStatus.UNPAID,
            ).delete()

        self.generate_due_installments()

    def create_next_period(self, from_date=None):
        """
        Create a single installment starting from `from_date`.
        Uses get_or_create so it is safe to call multiple times.
        """
        start = from_date or self.start_date

        installment, _created = FixedCostInstallment.objects.get_or_create(
            schedule=self,
            due_date=start,
            defaults={
                "company": self.company,
                "amount": self.amount,
            },
        )
        return installment

    def generate_due_installments(self):
        """
        Generate every installment that has become "due enough to track" -
        anything already due, plus anything due within HORIZON_DAYS from
        today. Safe to call repeatedly - uses get_or_create so existing
        installments are never overwritten or duplicated.

        Each due_date is computed directly from start_date (start_date +
        n periods), never by adding the period onto the previously computed
        date - a day-31 start date would otherwise drift to the 28th and
        stay there forever once relativedelta starts clamping short months.
        """
        from dateutil.relativedelta import relativedelta

        horizon = timezone.now().date() + datetime.timedelta(days=self.HORIZON_DAYS)
        n = 0
        while True:
            due_date = self.start_date + relativedelta(**{self.period_unit: self.period_length * n})
            if due_date > horizon:
                break
            FixedCostInstallment.objects.get_or_create(
                schedule=self,
                due_date=due_date,
                defaults={
                    "company": self.company,
                    "amount": self.amount,
                },
            )
            n += 1


class FixedCostInstallment(Timestampable, CompanyOwnedModel, Payable):
    schedule = models.ForeignKey(
        FixedCostSchedule,
        on_delete=models.PROTECT,
        related_name='installments',
        verbose_name=_("Fixed Cost Schedule"),
    )
    due_date = models.DateField(verbose_name=_("Due Date"))
    amount = MoneyField(
        max_digits=14, decimal_places=2, default_currency='EUR',
        verbose_name=_("Amount"),
    )
    payment_status = models.CharField(
        max_length=20,
        choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
        verbose_name=_("Payment Status"),
        help_text=_("Auto-calculated from the payments allocated to this installment."),
    )
    is_paid = models.BooleanField(default=False, verbose_name=_("Paid"))
    paid_via = models.CharField(
        max_length=20,
        choices=PaidVia.choices,
        blank=True,
        default="",
        verbose_name=_("Paid Via"),
        help_text=_("Empty until the installment is actually paid."),
    )
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Paid At"))
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name=_("Paid By"),
    )
    matched_invoice = models.ForeignKey(
        'invoices.Invoice',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name=_("Matched Invoice"),
        help_text=_(
            "The real invoice/receipt eventually loaded for this recurring cost, "
            "once confirmed as a match. Used to avoid double-counting this expense "
            "in the cashflow."
        ),
    )
    is_estimated = models.BooleanField(
        default=False,
        verbose_name=_("Is Estimated"),
        help_text=_(
            "Reserved for future variable-amount fixed costs (utilities, etc.), where "
            "the amount is a projection to be reconciled against the real expense later. "
            "Always False for fixed-amount costs like renting or rent."
        ),
    )

    class Meta:
        verbose_name = _("Fixed Cost Installment")
        verbose_name_plural = _("Fixed Cost Installments")
        ordering = ["-due_date"]
        unique_together = [["schedule", "due_date"]]

    def __str__(self):
        return f"{self.schedule.concept} | {self.due_date}"

    def save(self, *args, **kwargs):
        """
        Catches matched_invoice being changed or cleared by a direct field
        edit (e.g. the admin form, where it's a normal editable autocomplete
        field) - not just through FixedCostInvoiceMatcher.unmatch()/confirm().
        If it changed, this installment's mirror allocation (sourced from
        whichever invoice it was previously linked to) is stale and must go
        with it; deleting it triggers PaymentAllocation's own post_delete
        signal, which recalculates this installment's payment_status.
        """
        old_matched_invoice_id = None
        if self.pk:
            old_matched_invoice_id = (
                FixedCostInstallment.objects.filter(pk=self.pk)
                .values_list("matched_invoice_id", flat=True)
                .first()
            )

        super().save(*args, **kwargs)

        if old_matched_invoice_id and old_matched_invoice_id != self.matched_invoice_id:
            from payments.models import PaymentAllocation
            ct = ContentType.objects.get_for_model(self)
            PaymentAllocation.objects.filter(
                content_type=ct, object_id=self.pk, is_mirror=True,
            ).delete()

    @property
    def amount_paid(self) -> Money:
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(self)
        total = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or 0
        )
        return Money(total, self.amount_currency)

    @property
    def amount_remaining(self) -> Decimal:
        if self.amount:
            return (self.amount - self.amount_paid).amount
        return Decimal("0.00")

    def recalculate_payment_status(self):
        """
        Recompute payment_status (the ledger: how much of this installment is
        actually covered by confirmed PaymentAllocations) from scratch, mirroring
        Invoice.recalculate_payment_status(). Not wired to any signal yet — the
        bank matcher task will call this once it starts creating
        PaymentAllocations for installments.

        payment_status and is_paid answer two different questions and are
        deliberately allowed to disagree:
        - payment_status: how much of the ledger is covered right now.
        - is_paid: whether this installment should stop being chased by the
          Cashflow - which can be confirmed by a human before the ledger
          catches up, or by the ledger reaching full coverage before anyone
          confirms it by hand.

        So is_paid is only touched here in two cases:
        - The ledger just reached full coverage and nobody had confirmed
          payment yet -> confirm it automatically (paid_via=RECONCILIATION),
          same rule already used for payroll reconciliation.
        - The ledger used to be at full coverage but no longer is (e.g. an
          allocation got deleted) AND the confirmation that made it "paid" was
          this same automatic reconciliation -> undo it, since the ledger was
          its only basis. A MANUAL confirmation is never touched here - it
          came from a person, and a recalculation has no authority to
          overrule that.
        A PARTIAL ledger never affects is_paid either way.
        """
        total_applied = self.amount_paid.amount
        total = self.amount.amount if self.amount else Decimal("0.00")

        if total_applied <= 0:
            new_status = InvoicePaymentStatus.UNPAID
        elif total_applied >= total:
            new_status = InvoicePaymentStatus.PAID
        else:
            new_status = InvoicePaymentStatus.PARTIAL

        update_fields = ["payment_status", "updated_at"]

        if new_status == InvoicePaymentStatus.PAID and not self.is_paid:
            self.is_paid = True
            self.paid_via = PaidVia.RECONCILIATION
            self.paid_at = timezone.now()
            self.paid_by = None
            update_fields += ["is_paid", "paid_via", "paid_at", "paid_by"]
        elif (
            new_status != InvoicePaymentStatus.PAID
            and self.is_paid
            and self.paid_via == PaidVia.RECONCILIATION
        ):
            self.is_paid = False
            self.paid_via = ""
            self.paid_at = None
            self.paid_by = None
            update_fields += ["is_paid", "paid_via", "paid_at", "paid_by"]

        self.payment_status = new_status
        self.save(update_fields=update_fields)

    def mark_as_paid(self, user):
        """Manual confirmation - moved here from FixedCostInstallmentAdmin's
        mark_paid_action/mark_selected_as_paid, same fields, same values."""
        self.is_paid = True
        self.paid_at = timezone.now()
        self.paid_by = user
        self.paid_via = PaidVia.MANUAL
        self.save(update_fields=["is_paid", "paid_at", "paid_by", "paid_via", "updated_at"])

    def mark_as_unpaid(self):
        """Clears the manual confirmation - moved here from
        FixedCostInstallmentAdmin's mark_unpaid_action, same fields, same values."""
        self.is_paid = False
        self.paid_at = None
        self.paid_by = None
        self.paid_via = ""
        self.save(update_fields=["is_paid", "paid_at", "paid_by", "paid_via", "updated_at"])


class FixedCostInvoiceMatchSuggestionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    CONFIRMED = "confirmed", _("Confirmed")
    REJECTED = "rejected", _("Rejected")


class FixedCostInvoiceMatchSuggestion(Timestampable):
    """
    Candidate real Invoice proposed for a recurring FixedCostInstallment,
    pending human review. Regenerated from scratch on every matcher run for
    a given installment (old PENDING rows for that installment are deleted
    first - see banking.services.transaction_matching.fixed_cost_invoice_matcher).

    Confirming a suggestion sets FixedCostInstallment.matched_invoice - this
    row is never itself the source of truth for the match, same as
    invoices.AIMatchSuggestion / InvoicePOAllocation.

    A REJECTED suggestion only blocks that specific (installment, invoice)
    pair from being re-suggested - the invoice remains a normal candidate
    for any other installment, and even for this one if the schedule/invoice
    data changes and someone reruns the matcher expecting a fresh look
    (rejections aren't purged automatically, so re-suggesting the exact same
    pair is what's blocked, not the invoice in general).
    """
    installment = models.ForeignKey(
        FixedCostInstallment,
        on_delete=models.CASCADE,
        related_name="invoice_match_suggestions",
        verbose_name=_("Fixed Cost Installment"),
    )
    invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.CASCADE,
        related_name="fixedcost_match_suggestions",
        verbose_name=_("Invoice"),
    )
    confidence = models.CharField(
        max_length=10,
        default="high",
        verbose_name=_("Confidence"),
    )
    status = models.CharField(
        max_length=10,
        choices=FixedCostInvoiceMatchSuggestionStatus.choices,
        default=FixedCostInvoiceMatchSuggestionStatus.PENDING,
        verbose_name=_("Status"),
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Reviewed By"),
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Reviewed At"))

    class Meta:
        verbose_name = _("Fixed Cost Invoice Match Suggestion")
        verbose_name_plural = _("Fixed Cost Invoice Match Suggestions")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.installment} → {self.invoice} ({self.status})"