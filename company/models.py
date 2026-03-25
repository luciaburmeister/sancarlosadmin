from django.db import models
from django.utils.translation import gettext_lazy as _  
from django.contrib.auth import get_user_model
from django_countries.fields import CountryField
from django.contrib.postgres.fields import ArrayField
from location_field.models.spatial import LocationField
from simple_history.models import HistoricalRecords
from datetime import timedelta

User = get_user_model()


class Company(models.Model):
    name = models.CharField(max_length=100,verbose_name=_("Commercial Name"))
    legal_name = models.CharField(max_length=100, blank=True,verbose_name=_("Legal Name"))
    is_active = models.BooleanField(default=True,verbose_name=_("Activo"))
    logo = models.ImageField(verbose_name=_("Logo"),upload_to="logos")
    address = models.CharField(max_length=200,blank=True,null=True,verbose_name=_("Address"))
    email = models.CharField(max_length=80,blank=True,null=True,verbose_name=_("Email"))
    phone = models.CharField(max_length=18,blank=True,null=True,verbose_name=_("Phone"))
    tax_identifier = models.CharField(max_length=20,blank=True,verbose_name=_("Tax Identifier"),null=True)
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


    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("Company")

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

    class Meta:
        verbose_name = _("Employee")
        verbose_name_plural = _("Employees")
        ordering = ["last_name", "first_name"]
        unique_together = ("company", "identification_number")
        

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.company})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
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
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    street_address = models.CharField(max_length=255, verbose_name=_("Address"), default="")
    city = models.CharField(max_length=100, verbose_name=_("City"), default="")
    postal_code = models.CharField(max_length=20, verbose_name=_("Postal Code"), default="")
    province = models.CharField(max_length=100, verbose_name=_("Province"), default="")
    country = CountryField(verbose_name=_("Country"), default="ES")
    location = LocationField(based_fields=['street_address', 'city', 'postal_code', 'province', 'country'], zoom=18, default='POINT(36.7178 4.4256)')
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Working Zone")
        verbose_name_plural = _("Working Zones")

    def __str__(self):
        return f"{self.name} ({self.company})"

class BaseBankAccount(models.Model):
    bank_name = models.CharField(max_length=100, verbose_name=_("Bank Name"))
    holder_name = models.CharField(max_length=100, blank=True, verbose_name=_("Holder Name"))
    iban = models.CharField(max_length=34, blank=True, verbose_name=_("IBAN"), help_text=_("For SEPA/Europe"))
    swift_bic = models.CharField(max_length=11, blank=True, verbose_name=_("SWIFT/BIC code"))
    account_number = models.CharField(max_length=50, blank=True, verbose_name=_("Account Number"), help_text=_("Local account number"))
    routing_number = models.CharField(max_length=50, blank=True, verbose_name=_("Routing Number"), help_text=_("Routing number, Sort code, ABA, etc."))
    bank_address = models.TextField(blank=True, verbose_name=_("Bank Address"))
    country = CountryField(blank=True, null=True, verbose_name=_("Country"))

    class Meta:
        abstract = True

class BankAccount(CompanyOwnedModel, BaseBankAccount):
    is_default = models.BooleanField(default=False, verbose_name=_("Is Default"))

    class Meta:
        verbose_name = _("Bank Account")
        verbose_name_plural = _("Bank Accounts")

    def __str__(self):
        return f"{self.bank_name} - {self.account_number or self.iban} ({self.company})"

    def save(self, *args, **kwargs):
        if self.is_default:
            BankAccount.objects.filter(company=self.company, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

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
