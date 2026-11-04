import unicodedata
import uuid
from typing import Any
from collections.abc import Iterable
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.fields import GenericForeignKey
from django_countries.fields import CountryField
from django.contrib.postgres.fields import ArrayField
from location_field.models.spatial import LocationField
from django.core.exceptions import ValidationError
from timestamps.models import Timestampable
from simple_history.models import HistoricalRecords
from djmoney.models.fields import MoneyField
from datetime import date, timedelta

User = get_user_model()


def validate_work_pin(value):
    """Accept a 4-digit plaintext PIN (before hashing) or an existing Django hash."""
    if not value:
        return
    if value.isdigit() and len(value) == 4:
        return
    if "$" in value:
        return
    raise ValidationError(_("El PIN debe tener exactamente 4 dígitos numéricos."))


class DocumentLanguage(models.TextChoices):
    """Languages a client's documents (invoices, quotes, delivery orders…)
    can print in. Spanish is the default."""

    ES = "es", "Español"
    EN = "en", "English"
    FR = "fr", "Français"


class CompanyModule(models.TextChoices):
    """Optional areas of the ERP a company uses. Only decides which onboarding
    steps it is shown; it does not switch features on or off."""

    INVENTORY = "inventory", _("Inventory")
    PRODUCTION = "production", _("Production")
    ASSETS = "assets", _("Assets")
    PURCHASING = "purchasing", _("Purchasing")
    PAYROLL = "payroll", _("Payroll")
    EINVOICING = "einvoicing", _("E-invoicing")


class Company(models.Model):
    name = models.CharField(max_length=100,verbose_name=_("Commercial Name"))
    legal_name = models.CharField(max_length=100, blank=True,verbose_name=_("Legal Name"))
    is_active = models.BooleanField(default=True,verbose_name=_("Activo"))
    logo = models.ImageField(verbose_name=_("Logo"),upload_to="logos")
    address = models.CharField(max_length=200,blank=True,null=True,verbose_name=_("Address"))
    email = models.CharField(max_length=80,blank=True,null=True,verbose_name=_("Email"))
    phone = models.CharField(max_length=18,blank=True,null=True,verbose_name=_("Phone"))
    tax_identifier = models.CharField(max_length=20,blank=True,verbose_name=_("Tax Identifier"),null=True)
    industry_identifier = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Industry Identifier"),
        help_text=_("e.g., CNAE, SIC code, or similar industry classification"),
    )
    constituted_country = CountryField(null=True,verbose_name=_("Constitude Country"))
    sepa_creditor_id = models.CharField(
        max_length=35,
        blank=True,
        verbose_name=_("SEPA Creditor Identifier"),
        help_text=_("Given by the bank to collect by SEPA direct debit, e.g. ES12000B12345678."),
    )
    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Ordered list of possible production stages for this company"),
        verbose_name=_("Production Stages"),
    )
    pause_reasons = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_(
            "Reasons the production terminal offers when pausing a task "
            "(e.g. Waiting for parts). Leave empty to pause without a reason."
        ),
        verbose_name=_("Pause Reasons"),
    )
    asset_identifier_labels = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Possible labels for asset identifiers (e.g., VIN, Plate Number)"),
        verbose_name=_("Asset Identifier Labels"),
    )
    business_activity_description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Business Activity"),
        help_text=_("Description of what the company does"),
    )
    reply_to_email = models.EmailField(
        blank=True,
        verbose_name=_("Reply-To Email"),
        help_text=_(
            "Where replies to emails sent by the system land. Overridden per "
            "document type below, and by the sender's own address when a user "
            "triggers the send. Leave empty to reply to the sending mailbox."
        ),
    )
    document_reminder_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Document Reminder Emails"),
        help_text=_("Emails that will receive document expiration reminders"),
    )
    bank_summary_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Bank Summary Emails"),
        help_text=_("Emails that will receive the periodic bank accounts balance summary"),
    )
    purchase_order_notification_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Purchase Order Notification Emails"),
        help_text=_("Emails that will be notified whenever a new Purchase Order is created"),
    )
    invoice_digest_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Invoice Digest Emails"),
        help_text=_("Emails that will receive the daily summary of pending invoices to pay and to collect"),
    )
    unmatched_po_digest_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Unmatched PO Digest Emails"),
        help_text=_("Emails that will receive the daily summary of purchase invoices still not linked to a purchase order"),
    )
    unmatched_po_digest_grace_hours = models.PositiveIntegerField(
        default=48,
        verbose_name=_("Unmatched PO Digest Grace Hours"),
        help_text=_(
            "Hours an invoice may go without a linked purchase order before it is reported, "
            "counted from the moment it was registered in the ERP."
        ),
    )
    unmatched_po_digest_start_date = models.DateField(
        default=date(2026, 9, 1),
        null=True,
        blank=True,
        verbose_name=_("Unmatched PO Digest Start Date"),
        help_text=_(
            "Only invoices registered on or after this date are reported as missing a purchase order, "
            "so the digest is not flooded with historical invoices. Leave empty to report all invoices."
        ),
    )
    asset_plan_draft_notification_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Asset Plan Draft Notification Emails"),
        help_text=_("Emails notified whenever a new Asset Planning Request Draft is submitted from the mobile app"),
    )
    terms_text = models.TextField(
        blank=True,
        verbose_name=_("Términos y Condiciones"),
        help_text=_("Texto que el cliente debe leer y firmar al registrarse. Déjalo vacío para no requerir firma."),
    )
    allow_shared_production_stage_activities = models.BooleanField(
        default=True,
        verbose_name=_("Shared production stage activities"),
        help_text=_(
            "When enabled, multiple workers can join the same stage while it is in progress. "
            "Disable for companies where only one worker may run a stage at a time (e.g. Aluares)."
        ),
    )
    production_stage_undo_minutes = models.PositiveSmallIntegerField(
        default=15,
        verbose_name=_("Production stage undo window (minutes)"),
        help_text=_(
            "How long a worker can undo finishing a production stage they finished themselves. "
            "Use 0 to disable undo; production supervisors can still reopen stages."
        ),
    )
    auto_apply_client_taxes = models.BooleanField(
        default=False,
        verbose_name=_("Auto-apply client taxes"),
        help_text=_(
            "When enabled, taxes configured on the client replace product template taxes "
            "on new Configurations. Client taxes on inventory items are applied when added to a Quote."
        ),
    )
    bulk_emit_copy_quote_observations = models.BooleanField(
        default=False,
        verbose_name=_("Copy quote observations on bulk emit"),
        help_text=_(
            "When enabled, bulk-emitted invoices append the observations of each "
            "project file's accepted quotes to the invoice notes, matching the "
            "single-invoice flow. The API can override this per request."
        ),
    )
    default_taxes = models.ManyToManyField(
        "invoices.TaxType",
        through="company.CompanyDefaultTax",
        blank=True,
        related_name="companies_with_default",
        verbose_name=_("Default Taxes"),
        help_text=_(
            "Taxes automatically applied to new items/configurations "
            "when no other tax source exists."
        ),
    )
    invoice_number_format = models.CharField(
        max_length=100,
        default="INV-{year}-{number}",
        blank=True,
        verbose_name=_("Invoice Number Format"),
        help_text=_(
            "Format pattern for generated invoice numbers. "
            "Available placeholders: {year}, {number}. "
            "Default: INV-{year}-{number}"
        ),
    )
    proforma_number_format = models.CharField(
        max_length=100,
        default="PF-{year}-{number}",
        blank=True,
        verbose_name=_("Proforma Number Format"),
        help_text=_(
            "Format pattern for generated proforma numbers, a sequence separate "
            "from invoice numbers. Available placeholders: {year}, {number}. "
            "Default: PF-{year}-{number}"
        ),
    )
    debit_note_number_format = models.CharField(
        max_length=100,
        default="SUP-{year}-{number}",
        blank=True,
        verbose_name=_("Debit Note Number Format"),
        help_text=_(
            "Format pattern for debit notes charging another group company what "
            "this one paid for it, a sequence separate from invoice numbers. It "
            "must differ from the invoice format. Available placeholders: "
            "{year}, {number}. Default: SUP-{year}-{number}"
        ),
    )
    credit_note_number_format = models.CharField(
        max_length=100,
        default="R-{year}-{number}",
        blank=True,
        verbose_name=_("Credit Note Number Format"),
        help_text=_(
            "Format pattern for credit notes (facturas rectificativas) reversing "
            "an issued invoice, a sequence separate from invoice numbers. It "
            "must differ from the invoice and debit note formats. Available "
            "placeholders: {year}, {number}. Default: R-{year}-{number}"
        ),
    )
    require_asset_on_project_file = models.BooleanField(
        default=False,
        verbose_name=_("Require asset on project files"),
        help_text=_(
            "When enabled, new project files must have an asset (e.g. the vehicle "
            "entering the workshop) linked before they can be saved."
        ),
    )
    require_working_zone_on_project_file = models.BooleanField(
        default=False,
        verbose_name=_("Require working zone on project files"),
        help_text=_(
            "When enabled, new project files must have at least one working zone "
            "linked before they can be saved."
        ),
    )
    asset_reception_product = models.ForeignKey(
        "configurator.ProductTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Asset reception product"),
        help_text=_(
            "Shop-flow product template (price 0, excluded from invoicing) used to "
            "register asset receptions from a project file."
        ),
    )
    ideal_daily_work_minutes = models.PositiveIntegerField(
        default=480,
        verbose_name=_("Ideal daily work minutes"),
    )
    claude_api_key_encrypted = models.TextField(
        blank=True,
        verbose_name=_("Claude API key"),
        help_text=_("Stored encrypted. Set it from the company form."),
    )
    enabled_modules = ArrayField(
        base_field=models.CharField(max_length=20, choices=CompanyModule.choices),
        default=list,
        blank=True,
        verbose_name=_("Modules"),
        help_text=_("Optional areas this company uses; decides which setup steps it is shown."),
    )
    onboarding_completed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Setup completed"),
        help_text=_("When the company finished its setup. Empty while it is still being set up."),
    )

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        # Every series is numbered on its own: a shared format would
        # repeat invoice numbers within the company
        invoice_format = self.invoice_number_format or "INV-{year}-{number}"
        debit_note_format = self.debit_note_number_format or "SUP-{year}-{number}"
        credit_note_format = self.credit_note_number_format or "R-{year}-{number}"
        if debit_note_format == invoice_format:
            raise ValidationError({
                "debit_note_number_format": _("Debit notes need a number format different from invoices'."),
            })
        if credit_note_format in (invoice_format, debit_note_format):
            raise ValidationError({
                "credit_note_number_format": _(
                    "Credit notes need a number format different from invoices' and debit notes'."
                ),
            })

    def set_claude_api_key(self, plaintext: str) -> None:
        """Encrypt and store this company's Claude API key. Blank clears it."""
        from apps.einvoicing.encryption import encrypt_value

        plaintext = (plaintext or "").strip()
        if not plaintext:
            self.claude_api_key_encrypted = ""
            return
        self.claude_api_key_encrypted = encrypt_value(plaintext)

    def get_claude_api_key(self) -> str:
        """Return the decrypted Claude API key, or "" when none is stored."""
        from cryptography.fernet import InvalidToken

        from apps.einvoicing.encryption import decrypt_value

        if not self.claude_api_key_encrypted:
            return ""
        try:
            return decrypt_value(self.claude_api_key_encrypted)
        except InvalidToken:
            return ""

    def working_windows_for_day(self, day):
        """This weekday's schedule slots (start, end), in order — a weekday can have several (split shifts)."""
        return [
            (slot.start_time, slot.end_time)
            for slot in self.schedule_slots.filter(weekday=day.weekday()).order_by("start_time")
        ]

    def assert_within_working_hours(self, start, end):
        """Raise ValueError unless [start, end) fits inside one working window of its (local) day —
        or, for a multi-day range, unless *start* and *end* each fall inside a working window of
        their own (local) day (the hours in between are not validated)."""
        local_start = timezone.localtime(start)
        local_end = timezone.localtime(end)

        if local_start.date() == local_end.date():
            windows = self.working_windows_for_day(local_start.date())
            if not any(
                w_start <= local_start.time() and local_end.time() <= w_end
                for w_start, w_end in windows
            ):
                raise ValueError(_("That time falls outside working hours that day."))
            return

        start_windows = self.working_windows_for_day(local_start.date())
        if not any(w_start <= local_start.time() <= w_end for w_start, w_end in start_windows):
            raise ValueError(_("The start time falls outside working hours that day."))

        end_windows = self.working_windows_for_day(local_end.date())
        if not any(w_start <= local_end.time() <= w_end for w_start, w_end in end_windows):
            raise ValueError(_("The end time falls outside working hours that day."))

    class Meta:
        verbose_name = _("Company")
        permissions = [
            ("can_onboard_company", _("Can complete the setup of their company")),
        ]


class CompanyDefaultTax(models.Model):
    """Through model for Company.default_taxes — stores the selected percentage per company."""
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="company_default_taxes",
        verbose_name=_("Company"),
    )
    tax_type = models.ForeignKey(
        "invoices.TaxType",
        on_delete=models.CASCADE,
        related_name="company_defaults",
        verbose_name=_("Tax Type"),
    )
    selected_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name=_("Selected Percentage"),
    )

    class Meta:
        verbose_name = _("Company Default Tax")
        verbose_name_plural = _("Company Default Taxes")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "tax_type"],
                name="unique_company_default_tax",
            ),
        ]

    def __str__(self):
        return f"{self.company.name} — {self.tax_type} @ {self.selected_percentage}%"

    def save(self, *args: Any, **kwargs: Any) -> None:
        super().save(*args, **kwargs)
        self._rates_changed()

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        result = super().delete(*args, **kwargs)
        self._rates_changed()
        return result

    def _rates_changed(self) -> None:
        """Client discounts with volume thresholds include these taxes in shops showing prices with tax."""
        from apps.shopify.services.push_scheduler import ShopifyPushScheduler

        ShopifyPushScheduler.schedule_discount_sync([self.company_id])

    @classmethod
    def consumer_rates(cls, company_ids: Iterable[int]) -> dict[int, Decimal]:
        """Per company, the default tax percentage a consumer pays on top of a price.

        Only taxes that add to the price count; retentions apply between
        businesses. Companies without default taxes are left out.
        """
        rates: dict[int, Decimal] = {}
        for company_id, percentage, behavior in cls.objects.filter(company_id__in=company_ids).values_list(
            "company_id", "selected_percentage", "tax_type__behavior"
        ):
            rates[company_id] = rates.get(company_id, Decimal("0")) + (percentage if behavior == "add" else Decimal("0"))
        return rates

class CompanyOwnedModel(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        null=True,
        blank=False,
        verbose_name=_("Company")
    )

    class Meta:
        abstract = True

class CompanyUser(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="company_links"
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="user_links"
    )
    is_active = models.BooleanField(default=True)
    can_use_shared_terminal = models.BooleanField(
        default=False,
        verbose_name=_("Puede usar terminal compartida"),
        help_text=_(
            "Permite a esta cuenta gestionar sesiones de producción "
            "en nombre de cualquier empleado de la empresa."
        ),
    )

    class Meta:
        unique_together = ("user", "company")

    def __str__(self):
        return f"{self.user} → {self.company}"

class SexChoices(models.TextChoices):
    MALE = "m", _("Male")
    FEMALE = "f", _("Female")

class CivilStatusChoices(models.TextChoices):
    SINGLE = "single", _("Single")
    MARRIED = "married", _("Married")
    DIVORCED = "divorces", _("Divorced")

class EmployeeType(CompanyOwnedModel):
    type = models.CharField(max_length=50, verbose_name=_("Employee Type"))

    def __str__(self):
        return self.type

class Employee(CompanyOwnedModel):

    user = models.OneToOneField(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_profile",
        verbose_name=_("User Account")
    )
    
    can_work_on_production = models.BooleanField(
        default=False,
        verbose_name=_("Can work on production stages"),
    )

    first_name = models.CharField(
        max_length=50,
        verbose_name=_("First Name")
    )
    last_name = models.CharField(
        max_length=50,
        verbose_name=_("Last Name")
    )

    email = models.EmailField(
        blank=True,
        null=True,
        verbose_name=_("Work Email")
    )
    phone = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name=_("Phone")
    )

    hired_at = models.DateField(
        verbose_name=_("Hire Date")
    )
    terminated_at = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Termination Date")
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active Employee")
    )

    work_calendar = models.ForeignKey(
        "vacations.WorkCalendar",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employees",
        verbose_name=_("Work Calendar"),
    )

    identification_number = models.CharField(max_length=20,verbose_name=_("Identification Number"),null=True)

    birth_date = models.DateField(verbose_name=_("Birth Date"), null=True)

    sex = models.CharField(choices=SexChoices.choices,max_length=1,null=True, verbose_name=_("Sex"))

    civil_status = models.CharField(choices=CivilStatusChoices.choices,max_length=8,null=True,verbose_name=_("Civil Status"))

    birth_country = CountryField(null=True, default="ES",verbose_name=_("Birth Country"))

    type = models.ForeignKey(EmployeeType, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("Type"))

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        verbose_name=_("Assigned Production Stages"),
        help_text=_("Stages this employee is authorized to work on"),
    )

    sale_zones = models.ManyToManyField(
        "sales.SaleZone",
        blank=True,
        related_name="employees",
        verbose_name=_("Assigned Sale Zones")
    )

    # ── Personal home address ──────────────────────────────────────────────
    home_street_address = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        verbose_name=_("Street Address"),
    )
    home_city = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("City"),
    )
    home_postal_code = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        verbose_name=_("Postal Code"),
    )
    home_province = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Province"),
    )
    home_country = CountryField(
        blank=True,
        null=True,
        default="ES",
        verbose_name=_("Country"),
    )
    # ──────────────────────────────────────────────────────────────────────

    is_production_supervisor = models.BooleanField(
        default=False,
        verbose_name=_("Es supervisor de producción"),
        help_text=_("Puede asignar empleados a tareas desde la terminal compartida."),
    )
    work_pin = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        verbose_name=_("PIN de terminal (4 dígitos)"),
        help_text=_("Déjalo vacío para no cambiar el PIN actual. Introduce 4 dígitos para asignar uno nuevo."),
        validators=[validate_work_pin],
    )

    current_working_zone = models.ForeignKey(
        "company.WorkingZone",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_employees",
        verbose_name=_("Current Working Zone"),
    )
    zone_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Zone changed at"),
        help_text=_("When current_working_zone last actually changed."),
    )

    class Meta:
        verbose_name = _("Employee")
        verbose_name_plural = _("Employees")
        ordering = ["last_name", "first_name"]
        unique_together = ("company", "identification_number")

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def can_work_stage(self, stage_name: str) -> bool:
        """True when the employee's allowed stages include this one (or are unrestricted)."""
        stage_nfc = unicodedata.normalize("NFC", stage_name)
        allowed = [unicodedata.normalize("NFC", s) for s in (self.production_stages or [])]
        return not allowed or stage_nfc in allowed

    @classmethod
    def stage_restricted_worker(cls, user):
        """The user's shop-floor employee, whose production admin views are limited
        to their assigned stages; None for superusers, supervisors and office users."""
        if not user.is_authenticated or user.is_superuser:
            return None
        return cls.objects.filter(
            user=user,
            is_active=True,
            can_work_on_production=True,
            is_production_supervisor=False,
        ).first()

    @classmethod
    def can_reopen_production_stages(cls, user, company_id) -> bool:
        """Superusers and the company's active production supervisors may reopen finished stages."""
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return cls.objects.filter(
            user=user,
            company_id=company_id,
            is_active=True,
            is_production_supervisor=True,
        ).exists()

    def visible_production_stages(self, company_id) -> set[str]:
        """NFC stage names this worker may see on a ticket of *company_id*.
        Unlike can_work_stage(), no assigned stages means no visible stages."""
        if company_id != self.company_id:
            return set()
        return {unicodedata.normalize("NFC", s) for s in (self.production_stages or [])}

    def clean(self):
        super().clean()

        if self.company_id and self.production_stages:
            allowed = set(self.company.production_stages or [])
            invalid = [stage for stage in self.production_stages if stage not in allowed]

            if invalid:
                raise ValidationError({
                    "production_stages": _(
                        "These stages are not allowed for the selected company: %(stages)s"
                    ) % {"stages": ", ".join(invalid)}
                })

    def save(self, *args, **kwargs):
        if self.pk:
            old_zone_id = (
                Employee.objects.filter(pk=self.pk)
                .values_list("current_working_zone_id", flat=True)
                .first()
            )
            if old_zone_id != self.current_working_zone_id:
                self.zone_changed_at = timezone.now()
        elif self.current_working_zone_id:
            self.zone_changed_at = timezone.now()
        if self.work_pin and self.work_pin.isdigit() and len(self.work_pin) == 4:
            self.work_pin = make_password(self.work_pin)
        if self.home_city and not self.work_calendar_id:
            from vacations.models import WorkCalendar
            city_lower = self.home_city.strip().lower()
            if "madrid" in city_lower:
                calendar = WorkCalendar.objects.filter(city="madrid", is_active=True).first()
            elif "malaga" in city_lower or "málaga" in city_lower:
                calendar = WorkCalendar.objects.filter(city="malaga", is_active=True).first()
            else:
                calendar = None
            if calendar:
                self.work_calendar = calendar
        super().save(*args, **kwargs)

    
class MachineType(CompanyOwnedModel):
    type = models.CharField(max_length=50, verbose_name=_("Machine Type"))

    def __str__(self):
        return self.type

class Machine(CompanyOwnedModel):
    name = models.CharField(max_length=100, unique=True,verbose_name=_("Name"))

    code = models.CharField(
        max_length=50,
        unique=True,
        help_text=_("Internal or shop-floor identifier"),
    )
    
    type = models.ForeignKey(MachineType, on_delete=models.SET_NULL, null=True, blank=True, verbose_name=_("Type"))

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        verbose_name=_("Assigned Production Stages"),
        help_text=_("Stages this machine can work on"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Is active"))

    needs_human = models.BooleanField(default=True, verbose_name=_("Requires an operator"))
    
    daily_capacity_minutes = models.PositiveIntegerField(
        help_text=_("Total usable minutes per day"),
        verbose_name=_("Daily capacity in minutes")
    )

    parallel_capacity = models.PositiveSmallIntegerField(
        default=1,
        help_text=_("How many items can be processed in parallel"),
        verbose_name=_("Simultaneous jobs")
    )

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    def __str__(self):
        return self.name
    
    def clean(self):
        super().clean()

        if self.company_id and self.production_stages:
            allowed = set(self.company.production_stages or [])
            invalid = [stage for stage in self.production_stages if stage not in allowed]

            if invalid:
                raise ValidationError({
                    "production_stages": _(
                        "These stages are not allowed for the selected company: %(stages)s"
                    ) % {"stages": ", ".join(invalid)}
                })

class CompanyEmailConfig(CompanyOwnedModel):
    email_address = models.EmailField(verbose_name=_("Email Address"))
    imap_server = models.CharField(max_length=255, verbose_name=_("IMAP Server"))
    imap_port = models.PositiveIntegerField(default=993, verbose_name=_("IMAP Port"))
    imap_user = models.CharField(max_length=255, verbose_name=_("IMAP User"))
    imap_password = models.CharField(max_length=255, verbose_name=_("IMAP Password"))
    use_ssl = models.BooleanField(default=True, verbose_name=_("Use SSL"))
    last_checked = models.DateTimeField(null=True, blank=True, verbose_name=_("Last Checked"))

    class Meta:
        verbose_name = _("Company Email Configuration")
        verbose_name_plural = _("Company Email Configurations")

    def __str__(self):
        return f"{self.email_address} ({self.company})"

class WorkingZone(CompanyOwnedModel):

    class ZoneType(models.TextChoices):
        OBRA = "obra", _("Obra")
        VERTEDERO = "vertedero", _("Vertedero")
        ALMACEN = "almacen", _("Almacén")

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    zone_type = models.CharField(
        max_length=20,
        choices=ZoneType.choices,
        default=ZoneType.OBRA,
        verbose_name=_("Zone Type")
    )
    street_address = models.CharField(max_length=255, verbose_name=_("Address"), default="")
    city = models.CharField(max_length=100, verbose_name=_("City"), default="")
    postal_code = models.CharField(max_length=20, verbose_name=_("Postal Code"), default="")
    province = models.CharField(max_length=100, verbose_name=_("Province"), default="")
    country = CountryField(verbose_name=_("Country"), default="ES")
    cnae = models.CharField(
        max_length=10,
        blank=True,
        verbose_name=_("CNAE"),
        help_text=_("Optional activity code for this working zone."),
    )
    nima = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("NIMA"),
        help_text=_(
            "Optional environmental identification number "
            "(Número de Identificación Medioambiental)."
        ),
    )
    cif = models.CharField(
        max_length=20,
        blank=True,
        verbose_name=_("CIF"),
        help_text=_("Optional tax identifier of this working zone, printed on the transfer document."),
    )
    location = LocationField(based_fields=['street_address', 'city', 'postal_code', 'province', 'country'], zoom=18, default='POINT(36.7178 4.4256)')
    location_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Location confirmed at"),
        help_text=_("Set the first time a real-world transaction confirms this zone's GPS location."),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    receipt_notification_email = models.EmailField(
        blank=True,
        verbose_name=_("Receipt notification email"),
        help_text=_(
            "Inbox for the pickup document. The manual send action uses it, "
            "and a pickup that names this landfill is emailed here when automatic send is on."
        ),
    )
    auto_send_receipt = models.BooleanField(
        default=False,
        verbose_name=_("Send receipt automatically"),
        help_text=_("Email the pickup document when a pickup names this landfill."),
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="working_zones",
        verbose_name=_("Expediente"),
    )

    client = models.ForeignKey(
        'sales.Client',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="working_zones",
        verbose_name=_("Client")
    )

    class Meta:
        verbose_name = _("Working Zone")
        verbose_name_plural = _("Working Zones")

    def __str__(self):
        return f"{self.name} ({self.company})"

    def clean(self):
        super().clean()
        if self.zone_type == self.ZoneType.VERTEDERO:
            if self.auto_send_receipt and not self.receipt_notification_email:
                raise ValidationError({
                    "receipt_notification_email": _(
                        "An email is required to send the receipt automatically."
                    ),
                })
            return
        if self.receipt_notification_email or self.auto_send_receipt:
            raise ValidationError({
                "zone_type": _(
                    "Receipt email settings are only available for landfills."
                ),
            })

    def receipt_notification_recipient(self, *, require_auto=True):
        if self.zone_type != self.ZoneType.VERTEDERO:
            return ""
        email = (self.receipt_notification_email or "").strip()
        if not email or (require_auto and not self.auto_send_receipt):
            return ""
        return email

    @property
    def full_address(self) -> str:
        parts = [p for p in [self.street_address, self.city, self.postal_code] if p]
        return ", ".join(parts)

    def export_context(self) -> dict:
        return {
            "name": self.name,
            "zone_type": self.zone_type,
            "zone_type_display": self.get_zone_type_display(),
            "street_address": self.street_address,
            "city": self.city,
            "postal_code": self.postal_code,
            "province": self.province,
            "country": self.country.code if self.country else "",
            "full_address": self.full_address,
            "cnae": self.cnae,
            "nima": self.nima,
            "cif": self.cif,
        }

    def get_coordinates(self):
        """
        Returns (lat, lng) parsed from `location`, or (None, None) if unset/invalid.

        Longitude is normalized into [-180, 180): some stored points have
        picked up extra +/-360deg laps (e.g. from repeated location-picker
        searches), which is mathematically the same point but breaks any
        code that assumes a bounded coordinate range (bounding-box/zoom
        calculations blow up otherwise).
        """
        try:
            loc = self.location
            if hasattr(loc, "x") and hasattr(loc, "y"):
                lat, lon = loc.y, loc.x
            else:
                point_str = str(loc)
                if ";" in point_str:
                    point_str = point_str.split(";")[1]
                coords = point_str.strip().replace("POINT (", "").replace("POINT(", "").replace(")", "").split()
                lon = float(coords[0])
                lat = float(coords[1])
            lon = ((lon + 180) % 360) - 180
            if not (-90 <= lat <= 90):
                return None, None
            return lat, lon
        except Exception:
            return None, None


class WorkingZoneAuthorization(CompanyOwnedModel):
    """
    Which employees are authorized to fichar at a given WorkingZone.
    """
    working_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.CASCADE,
        related_name="authorizations",
        verbose_name=_("Working Zone"),
    )
    employee = models.ForeignKey(
        "company.Employee",
        on_delete=models.CASCADE,
        related_name="zone_authorizations",
        verbose_name=_("Employee"),
    )

    class Meta:
        verbose_name = _("Zone Authorization")
        verbose_name_plural = _("Zone Authorizations")
        unique_together = ("working_zone", "employee")
        ordering = ["employee__last_name", "employee__first_name"]

    def __str__(self):
        return f"{self.employee} → {self.working_zone}"


class WeekdayChoices(models.IntegerChoices):
    MONDAY = 0, _("Monday")
    TUESDAY = 1, _("Tuesday")
    WEDNESDAY = 2, _("Wednesday")
    THURSDAY = 3, _("Thursday")
    FRIDAY = 4, _("Friday")
    SATURDAY = 5, _("Saturday")
    SUNDAY = 6, _("Sunday")

class CompanyScheduleSlot(models.Model):
    company = models.ForeignKey(
        "Company",
        on_delete=models.CASCADE,
        related_name="schedule_slots",
        verbose_name=_("Company"),
    )
    weekday = models.IntegerField(
        choices=WeekdayChoices.choices,
        verbose_name=_("Weekday"),
    )
    start_time = models.TimeField(verbose_name=_("Start time"))
    end_time = models.TimeField(verbose_name=_("End time"))

    class Meta:
        verbose_name = _("Company schedule slot")
        verbose_name_plural = _("Company schedule")
        ordering = ["weekday", "start_time"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "weekday", "start_time", "end_time"],
                name="unique_company_weekday_time_slot",
            )
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.start_time} - {self.end_time}"

    def clean(self):
        super().clean()

        if self.end_time <= self.start_time:
            raise ValidationError({
                "end_time": _("End time must be after start time.")
            })

        qs = CompanyScheduleSlot.objects.filter(
            company=self.company,
            weekday=self.weekday,
        ).exclude(pk=self.pk)

        for slot in qs:
            overlaps = self.start_time < slot.end_time and self.end_time > slot.start_time
            if overlaps:
                raise ValidationError(
                    _("This time slot overlaps with another slot on the same day.")
                )

class BaseBankAccount(models.Model):
    bank_name = models.CharField(max_length=100, verbose_name=_("Bank Name"))
    holder_name = models.CharField(max_length=100, blank=True, verbose_name=_("Holder Name"))
    iban = models.CharField(max_length=50, blank=True, verbose_name=_("IBAN"), help_text=_("For SEPA/Europe"))
    swift_bic = models.CharField(max_length=11, blank=True, verbose_name=_("SWIFT/BIC code"))
    account_number = models.CharField(max_length=50, blank=True, verbose_name=_("Account Number"), help_text=_("Local account number"))
    routing_number = models.CharField(max_length=50, blank=True, verbose_name=_("Routing Number"), help_text=_("Routing number, Sort code, ABA, etc."))
    bank_address = models.TextField(blank=True, verbose_name=_("Bank Address"))
    country = CountryField(blank=True, null=True, verbose_name=_("Country"))

    class Meta:
        abstract = True

class ConsentStatus(models.TextChoices):
    UNKNOWN = "unknown", _("Unknown")
    VALID = "valid", _("Valid")
    INVALID = "invalid", _("Invalid")


class BankAccount(CompanyOwnedModel, BaseBankAccount):
    nuek_account_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Nuek Account ID"),
        help_text=_("External account identifier assigned by Nuek."),
    )
    nuek_token = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Nuek Token"),
        help_text=_("PSD2 token from Arcopay. Used to fetch transactions via /transactions/."),
    )
    nuek_consent_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Nuek Consent ID"),
        help_text=_("Consent identifier from Arcopay. Used to check consent status and refresh tokens."),
    )
    nuek_service = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Arcopay Bank Service"),
        help_text=_("Arcopay's identifier of the bank portal the consent was given at. Payments are sent through it."),
    )
    nuek_product_id = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Nuek Product ID"),
        help_text=_("Product identifier from Arcopay (IBAN or UUID). Used in /transactions/ calls."),
    )
    consent_status = models.CharField(
        max_length=10,
        choices=ConsentStatus.choices,
        default=ConsentStatus.UNKNOWN,
        verbose_name=_("Consent Status"),
        help_text=_("Whether the Arcopay PSD2 consent is still valid. Refreshed on every sync and via the 'Check Consent' admin action."),
    )
    consent_status_message = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("Consent Status Message"),
        help_text=_("Raw consent state reported by Arcopay (e.g. Valid, Rejected)."),
    )
    consent_valid_until = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Consent Valid Until"),
        help_text=_("Expiry date of the PSD2 consent as reported by Arcopay."),
    )
    consent_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Consent Checked At"),
        help_text=_("When the consent status was last verified against Arcopay."),
    )
    is_default = models.BooleanField(default=False, verbose_name=_("Is Default"))
    active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Included in cashflow balances and the bank summary email."),
    )
    sync_from_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Sync From Date"),
        help_text=_(
            "Fetch bank transactions starting from this date on the next sync. "
            "Used once: cleared automatically after a successful sync, after "
            "which the default window (last 90 days) applies again. "
            "Note: how far back the bank allows depends on the PSD2 consent."
        ),
    )
    last_known_balance = MoneyField(
        max_digits=14,
        decimal_places=2,
        default=None,
        default_currency="EUR",
        null=True,
        blank=True,
        verbose_name=_("Last Known Balance"),
        help_text=_("Balance reported by the bank on the last sync."),
    )
    last_balance_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Balance Updated At"),
        help_text=_("When the balance was last refreshed from the bank."),
    )

    account = models.ForeignKey(
        "accounting.CompanyAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bank_accounts",
        verbose_name=_("Accounting Account"),
        help_text=_("Created automatically when its first payment is journalized: the chart's bank root followed by the next number."),
    )

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Bank Account")
        verbose_name_plural = _("Bank Accounts")

    def __str__(self):
        return f"{self.bank_name} - {self.account_number or self.iban} ({self.company})"

    def save(self, *args, **kwargs):
        if self.is_default:
            BankAccount.objects.filter(company=self.company, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def upsert_from_arcopay(cls, company, product_data: dict, token: str, consent_id: str = ""):
        product_name = product_data.get("product", "")
        iban = product_data.get("iban", "")
        if not iban and len(product_name) >= 15 and not product_name.startswith("{"):
            iban = product_name
        holders = product_data.get("holders", [])
        holder_name = ""
        if holders and isinstance(holders, list) and isinstance(holders[0], dict):
            holder_name = holders[0].get("name", "")
        bank_type = product_data.get("type", "Bank")
        description = product_data.get("description", "")
        bank_name = f"{bank_type} - {description}" if description else bank_type
        lookup = {"company": company, "iban": iban} if iban else {"company": company, "nuek_product_id": product_name}
        defaults = {
            "nuek_token": token,
            "nuek_product_id": product_name,
        }
        if consent_id:
            defaults["nuek_consent_id"] = consent_id
        if holder_name:
            defaults["holder_name"] = holder_name
        if bank_name:
            defaults["bank_name"] = bank_name
        return cls.objects.update_or_create(**lookup, defaults=defaults)

class WorkAttendance(CompanyOwnedModel, Timestampable):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="attendances",
        verbose_name=_("Employee")
    )
    clock_in_time = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Clock In Time")
    )
    clock_out_time = models.DateTimeField(
        null=True, blank=True,
        verbose_name=_("Clock Out Time")
    )
    clock_in_lat = models.DecimalField(
        max_digits=9, decimal_places=6,
        null=True, blank=True,
        verbose_name=_("Clock In Latitude")
    )
    clock_in_lng = models.DecimalField(
        max_digits=9, decimal_places=6,
        null=True, blank=True,
        verbose_name=_("Clock In Longitude")
    )
    clock_out_lat = models.DecimalField(
        max_digits=9, decimal_places=6,
        null=True, blank=True,
        verbose_name=_("Clock Out Latitude")
    )
    clock_out_lng = models.DecimalField(
        max_digits=9, decimal_places=6,
        null=True, blank=True,
        verbose_name=_("Clock Out Longitude")
    )

    class Meta:
        verbose_name = _("Work Attendance")
        verbose_name_plural = _("Work Attendances")
        ordering = ["-clock_in_time"]

    def __str__(self):
        return f"{self.employee} — {self.clock_in_time}"
    
class LocationRecord(CompanyOwnedModel, Timestampable):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="location_records",
        verbose_name=_("Employee")
    )
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6,
        verbose_name=_("Latitude")
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6,
        verbose_name=_("Longitude")
    )
    recorded_at = models.DateTimeField(
        verbose_name=_("Recorded At")
    )

    class Meta:
        verbose_name = _("Location Record")
        verbose_name_plural = _("Location Records")
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.employee} — {self.recorded_at}"

class BaseDocumentType(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    periodicity_days = models.PositiveIntegerField(
        null=True, 
        blank=True, 
        verbose_name=_("Periodicity (days)"),
        help_text=_("Number of days this document type is valid for.")
    )
    notify_before_days = models.PositiveIntegerField(
        default=30, 
        verbose_name=_("Notify before (days)"),
        help_text=_("How many days before expiry to send a reminder.")
    )

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.name} ({self.company})"

class BaseDocument(CompanyOwnedModel):
    file = models.FileField(upload_to="documents/%Y/%m/%d/", verbose_name=_("File"))
    expiry_date = models.DateField(null=True, blank=True, verbose_name=_("Expiry Date"))
    issue_date = models.DateField(null=True, blank=True, verbose_name=_("Issue Date"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    is_active = models.BooleanField(default=True, verbose_name=_("Is Active"))
    does_not_expire = models.BooleanField(default=False, verbose_name=_("Does not expire"))
    reminder_sent_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Reminder Sent At"))
    
    history = HistoricalRecords(inherit=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        doc_type = getattr(self, "document_type", None)
        if doc_type and doc_type.periodicity_days and self.issue_date and not self.expiry_date and not self.does_not_expire:
            self.expiry_date = self.issue_date + timedelta(days=doc_type.periodicity_days)
        super().save(*args, **kwargs)

class EmployeeDocumentType(BaseDocumentType):
    class Meta:
        verbose_name = _("Employee Document Type")
        verbose_name_plural = _("Employee Document Types")

class EmployeeDocument(BaseDocument):
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("Employee")
    )
    document_type = models.ForeignKey(
        EmployeeDocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name=_("Document Type")
    )

    class Meta:
        verbose_name = _("Employee Document")
        verbose_name_plural = _("Employee Documents")

    def __str__(self):
        return f"{self.document_type.name} - {self.employee}"

    def save(self, *args, **kwargs):
        if not self.company_id and self.employee:
            self.company = self.employee.company
        super().save(*args, **kwargs)

class MachineDocumentType(BaseDocumentType):
    class Meta:
        verbose_name = _("Machine Document Type")
        verbose_name_plural = _("Machine Document Types")

class MachineDocument(BaseDocument):
    machine = models.ForeignKey(
        Machine,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("Machine")
    )
    document_type = models.ForeignKey(
        MachineDocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name=_("Document Type")
    )

    class Meta:
        verbose_name = _("Machine Document")
        verbose_name_plural = _("Machine Documents")

    def __str__(self):
        return f"{self.document_type.name} - {self.machine}"

    def save(self, *args, **kwargs):
        if not self.company_id and self.machine:
            self.company = self.machine.company
        super().save(*args, **kwargs)

class CompanyDocumentType(BaseDocumentType):
    class Meta:
        verbose_name = _("Company Document Type")
        verbose_name_plural = _("Company Document Types")

class CompanyDocument(BaseDocument):
    document_type = models.ForeignKey(
        CompanyDocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name=_("Document Type")
    )

    class Meta:
        verbose_name = _("Company Document")
        verbose_name_plural = _("Company Documents")

    def __str__(self):
        return f"{self.document_type.name} - {self.company}"


class EmailLog(CompanyOwnedModel):
    """
    Audit log for emails sent from the system (invoices, quotes, etc.).
    Uses a GenericForeignKey to link to any document model.
    """

    class EmailStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")

    class DeliveryStatus(models.TextChoices):
        UNKNOWN = "unknown", _("Not confirmed")
        ACCEPTED = "accepted", _("Accepted by provider")
        DELAYED = "delayed", _("Delivery delayed")
        DELIVERED = "delivered", _("Delivered to recipient server")
        BOUNCED = "bounced", _("Bounced")
        COMPLAINT = "complaint", _("Marked as spam")
        REJECTED = "rejected", _("Rejected")

    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.CASCADE,
        verbose_name=_("Content Type"),
    )
    object_id = models.PositiveIntegerField(verbose_name=_("Object ID"))

    content_object = GenericForeignKey("content_type", "object_id")

    batch_id = models.UUIDField(
        default=uuid.uuid4,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Email Batch ID"),
        help_text=_("Groups recipients created by the same send operation."),
    )
    email_document = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name=_("Email Document Type"),
    )

    to_email = models.CharField(
        max_length=500,
        verbose_name=_("To Email"),
        help_text=_("Comma-separated list of recipient emails"),
    )
    subject = models.CharField(max_length=500, verbose_name=_("Subject"))
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Sent At"))
    status = models.CharField(
        max_length=10,
        choices=EmailStatus.choices,
        default=EmailStatus.SUCCESS,
        verbose_name=_("Status"),
    )
    error_message = models.TextField(
        blank=True,
        verbose_name=_("Error Message"),
    )
    tracking_enabled = models.BooleanField(
        default=False,
        verbose_name=_("Tracking Enabled"),
    )
    provider = models.CharField(
        max_length=30,
        blank=True,
        verbose_name=_("Email Provider"),
    )
    provider_message_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name=_("Provider Message ID"),
    )
    delivery_status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.UNKNOWN,
        db_index=True,
        verbose_name=_("Delivery Status"),
    )
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Accepted At"),
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Delivered At"),
    )
    first_opened_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("First Opening Detected At"),
    )
    last_opened_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last Opening Detected At"),
    )
    open_count = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Detected Openings"),
    )
    last_event_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Last Provider Event At"),
    )
    delivery_error = models.TextField(
        blank=True,
        verbose_name=_("Delivery Error"),
    )

    class Meta:
        verbose_name = _("Email Log")
        verbose_name_plural = _("Email Logs")
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.subject} → {self.to_email} ({self.get_status_display()})"


class EmailEvent(models.Model):
    """Immutable provider event used for auditing and webhook idempotency."""

    class EventType(models.TextChoices):
        SEND = "send", _("Send")
        DELIVERY = "delivery", _("Delivery")
        DELIVERY_DELAY = "delivery_delay", _("Delivery Delay")
        BOUNCE = "bounce", _("Bounce")
        COMPLAINT = "complaint", _("Complaint")
        REJECT = "reject", _("Reject")
        RENDERING_FAILURE = "rendering_failure", _("Rendering Failure")
        OPEN = "open", _("Open")
        CLICK = "click", _("Click")
        SUBSCRIPTION = "subscription", _("Subscription")

    email_log = models.ForeignKey(
        EmailLog,
        on_delete=models.CASCADE,
        related_name="events",
        verbose_name=_("Email Log"),
    )
    provider = models.CharField(max_length=30, verbose_name=_("Provider"))
    provider_event_id = models.CharField(
        max_length=255,
        verbose_name=_("Provider Event ID"),
    )
    event_type = models.CharField(
        max_length=30,
        choices=EventType.choices,
        verbose_name=_("Event Type"),
    )
    occurred_at = models.DateTimeField(verbose_name=_("Occurred At"))
    recipient = models.EmailField(blank=True, verbose_name=_("Recipient"))
    details = models.TextField(blank=True, verbose_name=_("Details"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Email Event")
        verbose_name_plural = _("Email Events")
        ordering = ["occurred_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_event_id"],
                name="uniq_email_event_provider_id",
            ),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} — {self.email_log}"


class EmailReplyTo(CompanyOwnedModel):
    """
    Where replies to one kind of outgoing email should land for this company.

    Falls back to `Company.reply_to_email` when a document type has no row, and
    is itself overridden when a specific user triggered the send — see
    `general.email.reply_to`.
    """
    email_document = models.CharField(
        max_length=100,
        verbose_name=_("Document Type"),
        help_text=_("The kind of email this Reply-To applies to"),
    )
    reply_to_email = models.EmailField(
        verbose_name=_("Reply-To Email"),
        help_text=_("Address that receives replies to this kind of email"),
    )

    class Meta:
        verbose_name = _("Email Reply-To")
        verbose_name_plural = _("Email Reply-To Addresses")
        unique_together = ("company", "email_document")
        ordering = ["email_document"]

    def __str__(self):
        from general.email import document_label

        return f"{document_label(self.email_document)} → {self.reply_to_email}"


class ConfigurationList(CompanyOwnedModel):
    """
    Generic key-value list for company-specific configuration.
    """
    key = models.CharField(
        max_length=100,
        verbose_name=_("Key"),
        help_text=_("Identifier used by the app to fetch this list, e.g. cuba_material_types")
    )
    values = ArrayField(
        base_field=models.CharField(max_length=200),
        default=list,
        blank=True,
        verbose_name=_("Values"),
        help_text=_("List of options")
    )

    class Meta:
        verbose_name = _("Configuration List")
        verbose_name_plural = _("Configuration Lists")
        unique_together = ("company", "key")
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} ({self.company})"
