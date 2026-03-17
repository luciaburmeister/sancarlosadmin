from decimal import Decimal
from datetime import timedelta 

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.conf import settings
from django.utils import timezone

from timestamps.models import Timestampable
from company.models import Employee
from django_countries.fields import CountryField
from datetime import date


class VacationPolicy(models.Model):
    code = models.CharField(max_length=10, unique=True, verbose_name=_("Code"))
    name = models.CharField(max_length=120, verbose_name=_("Name"))
    country = CountryField(verbose_name=_("Country"))

    yearly_days = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("22.0"),
        validators=[MinValueValidator(Decimal("0.0"))],
        help_text=_("Days granted per year (pro rata)"),
        verbose_name=_("Yearly days"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    carry_over_max = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.0"),
        help_text=_("Max days that can be carried to next year"),
        verbose_name=_("Carry over max"),
    )

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        ordering = ("country", "code")
        verbose_name = _("Vacation policy")
        verbose_name_plural = _("Vacation policies")

    def __str__(self):
        return f"{self.name} ({self.country})"

    @property
    def annual_allowance_days(self):
        return self.yearly_days

    @property
    def carryover_max_days(self):
        return self.carry_over_max

class PolicyAbsenceType(models.Model):
    policy = models.ForeignKey(
        VacationPolicy,
        on_delete=models.CASCADE,
        related_name="absence_types",
        verbose_name=_("Policy")
    )

    code = models.CharField(max_length=30, verbose_name=_("Code"))
    name = models.CharField(max_length=120, verbose_name=_("Name"))

    consumes_vacation = models.BooleanField(default=True, verbose_name=_("Consumes vacation"))
    requires_approval = models.BooleanField(default=True, verbose_name=_("Requires approval"))
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("policy", "code")
        ordering = ("name",)

    def __str__(self):
        return self.name


class VacationWallet(Timestampable):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="vacation_wallets",
        verbose_name=_("Employee")
    )
    policy = models.ForeignKey(
        VacationPolicy,
        on_delete=models.PROTECT,
        related_name="wallets",
        verbose_name=_("Policy")
    )
    year = models.PositiveSmallIntegerField(help_text=_("Year this wallet applies to"), verbose_name=_("Year"),)

    accrued_ytd = models.DecimalField(
        max_digits=7, 
        decimal_places=2, 
        default=Decimal("0.00"), 
        verbose_name=_("Accrued ytd"),)
    
    used_ytd = models.DecimalField(max_digits=7, 
                                   decimal_places=2, 
                                   default=Decimal("0.00"),
                                   verbose_name=_("Used ytd"))
    
    balance = models.DecimalField(max_digits=7, 
                                  decimal_places=2, 
                                  default=Decimal("0.00"),
                                  verbose_name=_("Balance"))

    class Meta:
        unique_together = ("employee", "policy", "year")
        ordering = ("employee__id", "year")
        verbose_name = _("Vacation wallet")
        verbose_name_plural = _("Vacation wallets")

    def __str__(self):
        return f"{self.employee} — {self.policy.code} — {self.year}"

    @property
    def computed_balance(self):
        return self.balance

    def recompute_from_movements(self):
        movements = self.movements.all()

        accrued = Decimal("0.00")
        used = Decimal("0.00")
        adjustment_total = Decimal("0.00")

        for m in movements:
            if m.movement_type == VacationMovement.ACQUISITION:
                accrued += m.days
            elif m.movement_type == VacationMovement.CONSUMPTION:
                used += m.days
            elif m.movement_type == VacationMovement.ADJUSTMENT:
                adjustment_total += m.days

        balance = accrued + adjustment_total - used

        self.accrued_ytd = accrued + adjustment_total
        self.used_ytd = used
        self.balance = balance
        self.save(update_fields=["accrued_ytd", "used_ytd", "balance", "updated_at"])
        return self.balance
    
    def save(self, *args, **kwargs):
        creating = self.pk is None
        super().save(*args, **kwargs)

        if creating:
            if not self.movements.filter(
                movement_type=VacationMovement.ACQUISITION,
                reason__startswith="Initial yearly grant",
            ).exists():
                VacationMovement.objects.create(
                    wallet=self,
                    date=date(self.year, 1, 1),
                    movement_type=VacationMovement.ACQUISITION,
                    days=self.policy.yearly_days,
                    reason=f"Initial yearly grant ({self.year})",
                    created_by=None,
                )


class VacationMovement(Timestampable):
    ACQUISITION = "acquisition"
    CONSUMPTION = "consumption"
    ADJUSTMENT = "adjustment"

    MOVEMENT_CHOICES = (
        (ACQUISITION, _("Accrual / Grant")),
        (CONSUMPTION, _("Taken / Consumption")),
        (ADJUSTMENT, _("Adjustment")),
    )

    wallet = models.ForeignKey(
        VacationWallet,
        on_delete=models.CASCADE,
        related_name="movements",
    )

    date = models.DateField(db_index=True)

    movement_type = models.CharField(
        max_length=20,
        choices=MOVEMENT_CHOICES,
        db_index=True,
    )

    days = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text=_("Use positive values for acquisition/consumption. Adjustment may be positive or negative."),
    )

    reason = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vacation_movements_created",
    )

    class Meta:
        ordering = ("-date", "-created_at")
        verbose_name = _("Vacation movement")
        verbose_name_plural = _("Vacation movements")

    def __str__(self):
        sign = ""
        if self.movement_type == self.ACQUISITION and self.days > 0:
            sign = "+"
        elif self.movement_type == self.ADJUSTMENT and self.days > 0:
            sign = "+"
        return f"{self.get_movement_type_display()} {sign}{self.days}d"

    def clean(self):
        if self.movement_type in {self.ACQUISITION, self.CONSUMPTION} and self.days < 0:
            raise ValidationError(_("Acquisition and consumption movements must use positive days."))

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
        self.wallet.recompute_from_movements()

    def delete(self, *args, **kwargs):
        wallet = self.wallet
        super().delete(*args, **kwargs)
        wallet.recompute_from_movements()


class VacationRequestStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    CANCELLED = "cancelled", _("Cancelled")


class AbsenceReason(models.TextChoices):
    ANNUAL = "annual", _("Annual leave")
    PERSONAL = "personal", _("Personal day")
    SICK = "sick", _("Sick leave")
    MEDICAL = "medical", _("Medical appointment")
    FAMILY = "family", _("Family emergency")
    MARRIAGE = "marriage", _("Marriage leave")
    BEREAVEMENT = "bereavement", _("Bereavement leave")
    MATERNITY = "maternity", _("Maternity leave")
    PATERNITY = "paternity", _("Paternity leave")
    UNPAID = "unpaid", _("Unpaid leave")
    OTHER = "other", _("Other")


class VacationRequest(Timestampable):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="vacation_requests",
        verbose_name=_("Employee")
    )

    wallet = models.ForeignKey(
        VacationWallet,
        on_delete=models.PROTECT,
        related_name="requests",
        null=True,
        blank=True,
        verbose_name=_("Wallet")
    )

    start_date = models.DateField(verbose_name=_("Start date"))
    end_date = models.DateField(verbose_name=_("End date"))

    requested_days = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Requested days"),
    )

    reason = models.CharField(
        max_length=20,
        choices=AbsenceReason.choices,
        default=AbsenceReason.ANNUAL,
        verbose_name=_("Reason"),
    )

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    status = models.CharField(
        max_length=20,
        choices=VacationRequestStatus.choices,
        default=VacationRequestStatus.PENDING,
        db_index=True,
        verbose_name=_("Status"),
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vacation_requests_approved",
        verbose_name=_("Approved by"),
    )

    approved_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Approved at"))

    absence_type = models.ForeignKey(
        PolicyAbsenceType,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="requests",
    )

    class Meta:
        ordering = ("-created_at",)
        permissions = [
            ("can_view_all_vacations", "Can view all vacation requests (company-wide)"),
            ("can_approve_vacations", "Can approve/reject vacation requests"),
        ]
        verbose_name = _("Vacation request")
        verbose_name_plural = _("Vacation requests")

    def __str__(self):
        return f"{self.employee} {self.start_date}→{self.end_date} ({self.status})"

    def clean(self):
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(_("End date must be after start date."))

        if self.wallet and self.wallet.employee_id != self.employee_id:
            raise ValidationError(_("Wallet must belong to employee."))

    def compute_working_days(self) -> Decimal:
        if not self.start_date or not self.end_date:
            return Decimal("0.00")

        d = self.start_date
        total = 0
        while d <= self.end_date:
            if d.weekday() < 5:  
                total += 1
            d += timedelta(days=1)

        return Decimal(total).quantize(Decimal("0.00"))

    def approve(self, approver_user):
        if self.status == VacationRequestStatus.APPROVED:
            return

        if not self.wallet_id:
            raise ValidationError(_("Request has no wallet assigned."))

        self.requested_days = self.compute_working_days()

        if self.wallet.balance - self.requested_days < 0:
            raise ValidationError(_("Insufficient vacation balance. Cannot approve this request."))

        self.status = VacationRequestStatus.APPROVED
        self.approved_by = approver_user
        self.approved_at = timezone.now()
        self.save(update_fields=["requested_days", "status", "approved_by", "approved_at", "updated_at"])

        VacationMovement.objects.create(
            wallet=self.wallet,
            date=self.start_date,
            movement_type=VacationMovement.CONSUMPTION,
            days=self.requested_days,
            reason=f"Vacation {self.start_date}→{self.end_date}",
            created_by=approver_user,
        )
        
    def cancel(self, cancelled_by=None):
        today = timezone.now().date()

        if self.status not in (VacationRequestStatus.PENDING, VacationRequestStatus.APPROVED):
            return

        if self.end_date < today:
            raise ValidationError(_("Past requests cannot be cancelled."))

        if self.status == VacationRequestStatus.APPROVED and self.wallet_id:
            VacationMovement.objects.create(
                wallet=self.wallet,
                date=today,
                movement_type=VacationMovement.ADJUSTMENT,
                days=self.requested_days,
                reason=f"Cancellation reversal for request {self.start_date}→{self.end_date}",
                created_by=cancelled_by,
            )

        self.status = VacationRequestStatus.CANCELLED
        self.save(update_fields=["status", "updated_at"])

    def save(self, *args, **kwargs):
        if self.wallet_id is None and self.employee_id is not None and self.start_date:
            self.wallet = (
                VacationWallet.objects
                .filter(employee=self.employee, year=self.start_date.year)
                .order_by("-id")
                .first()
            )

        if self.start_date and self.end_date:
            self.requested_days = self.compute_working_days()

        super().save(*args, **kwargs)