from django.db import models
from django.utils.translation import gettext_lazy as _  
from django.contrib.auth import get_user_model
from django_countries.fields import CountryField

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


    def __str__(self):
        return self.name

class CompanyOwnedModel(models.Model):
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        null=True,
        blank=False
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
    
class Machine(CompanyOwnedModel):
    name = models.CharField(max_length=100, unique=True,verbose_name=_("Name"))

    code = models.CharField(
        max_length=50,
        unique=True,
        help_text=_("Internal or shop-floor identifier"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Is active"))
    
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
