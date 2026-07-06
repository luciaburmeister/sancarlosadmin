from django.contrib.auth.hashers import make_password
from django.db import models
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
from datetime import timedelta

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
    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Ordered list of possible production stages for this company"),
        verbose_name=_("Production Stages"),
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
    document_reminder_emails = ArrayField(
        base_field=models.EmailField(),
        default=list,
        blank=True,
        verbose_name=_("Document Reminder Emails"),
        help_text=_("Emails that will receive document expiration reminders"),
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
    auto_apply_client_taxes = models.BooleanField(
        default=False,
        verbose_name=_("Auto-apply client taxes"),
        help_text=_(
            "When enabled, taxes configured on the client replace product template taxes "
            "on new Configurations. Client taxes on inventory items are applied when added to a Quote."
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

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("Company")


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

    needs_human = models.BooleanField(default=True, verbose_name=_("Needs human"))
    
    daily_capacity_minutes = models.PositiveIntegerField(
        help_text=_("Total usable minutes per day"),
        verbose_name=_("Daily capacity in minutes")
    )

    parallel_capacity = models.PositiveSmallIntegerField(
        default=1,
        help_text=_("How many items can be processed in parallel"),
        verbose_name=_("Parallel capacity in minutes")
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
    imap_password = models.CharField(max_length=255, verbose_name=_("IMAP Password"))  # TODO: Encrypt this
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
    location = LocationField(based_fields=['street_address', 'city', 'postal_code', 'province', 'country'], zoom=18, default='POINT(36.7178 4.4256)')
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
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

        # Prevent overlaps in the same company and weekday
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
    nuek_product_id = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Nuek Product ID"),
        help_text=_("Product identifier from Arcopay (IBAN or UUID). Used in /transactions/ calls."),
    )
    is_default = models.BooleanField(default=False, verbose_name=_("Is Default"))
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
        """Create or update a BankAccount from an Arcopay product dict.

        Looks up by company + IBAN (or product_id for banks like Revolut
        that use UUIDs instead of IBANs).

        Returns (bank_account, created).
        """
        product_name = product_data.get("product", "")

        # The real IBAN may be in a dedicated 'iban' field (e.g. Revolut),
        # or in 'product' for traditional banks.
        iban = product_data.get("iban", "")
        if not iban and len(product_name) >= 15 and not product_name.startswith("{"):
            iban = product_name

        # Extract holder name from holders array
        holders = product_data.get("holders", [])
        holder_name = ""
        if holders and isinstance(holders, list) and isinstance(holders[0], dict):
            holder_name = holders[0].get("name", "")

        # Build a descriptive bank name
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
        SUCCESS = "success", _("Success")
        FAILED = "failed", _("Failed")

    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.CASCADE,
        verbose_name=_("Content Type"),
    )
    object_id = models.PositiveIntegerField(verbose_name=_("Object ID"))

    content_object = GenericForeignKey("content_type", "object_id")

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

    class Meta:
        verbose_name = _("Email Log")
        verbose_name_plural = _("Email Logs")
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.subject} → {self.to_email} ({self.get_status_display()})"


class ConfigurationList(CompanyOwnedModel):
    """
    Generic key-value list for company-specific configuration.
    Used to store dynamic option lists that can be managed from the admin
    without touching code.

    Examples:
      key="cuba_material_types"  values=["Agua", "Pladur", "RCD Limpio", ...]

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
