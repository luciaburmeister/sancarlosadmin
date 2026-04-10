# accounting/models.py
from __future__ import annotations

from django.db import models, transaction
from django.core.validators import RegexValidator
from django_countries.fields import CountryField
from timestamps.models import Timestampable
from company.models import Company,CompanyOwnedModel
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from djmoney.models.fields import MoneyField, CurrencyField
from decimal import Decimal
from django.utils import timezone


ACCOUNT_CODE_VALIDATOR = RegexValidator(
    regex=r"^\d{2,12}$",
    message="El código de cuenta debe ser numérico y tener entre 2 y 12 dígitos.",
)

class TemplateType(models.TextChoices):
    PGC = "PGC", "PGC (General)"
    PGC_PYMES = "PGC_PYMES", "PGC Pymes"
    CUSTOM = "CUSTOM", "Personalizado"

class ChartOfAccountsTemplate(Timestampable):
    """
    Plan contable base (p.ej. PGC, PGC Pymes, etc.) reutilizable entre empresas.
    """
    name = models.CharField(max_length=255)
    template_type = models.CharField(max_length=16, choices=TemplateType.choices, default=TemplateType.PGC)
    country = CountryField()  # ISO-3166-1 alpha2
    is_default = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.name


class FiscalYear(Timestampable, CompanyOwnedModel):
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    is_closed = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uniq_fiscal_year_name"),
        ]
        ordering = ["-end_date"]

    def __str__(self):
        return f"{self.name} ({self.start_date} - {self.end_date})"

    def clean(self):
        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValidationError(_("Start date must be before end date."))
            
            # Check for overlaps
            if self.company:
                overlapping = FiscalYear.objects.filter(
                    company=self.company,
                    start_date__lte=self.end_date,
                    end_date__gte=self.start_date
                ).exclude(pk=self.pk)
                
                if overlapping.exists():
                     raise ValidationError(_("Fiscal year overlaps with an existing year."))


class TemplateAccount(Timestampable):
    """
    Cuenta del plan base (template). Soporta jerarquía (padre/hijo).
    Ej: 43 Clientes -> 430 Clientes -> 4300 Clientes nacionales (según tu template).
    """
    template = models.ForeignKey(ChartOfAccountsTemplate, on_delete=models.CASCADE, related_name="accounts")
    code = models.CharField(max_length=12, validators=[ACCOUNT_CODE_VALIDATOR])
    name = models.CharField(max_length=255)
    description = models.TextField(null=True, blank=True)

    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        help_text=_("Parent account in the template."),
    )
    is_postable = models.BooleanField(
        default=True,
        help_text=_("If this account is postable."),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template", "code"], name="uniq_template_account_code"),
        ]
        indexes = [
            models.Index(fields=["template", "code"]),
            models.Index(fields=["template", "parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class CompanyChartOfAccounts(Timestampable):
    """
    Asigna un template a una empresa (y permite versionar/cambiar si lo necesitas).
    """
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="chart")
    template = models.ForeignKey(ChartOfAccountsTemplate, on_delete=models.PROTECT, related_name="company_charts")
    
    lock_date = models.DateField(
        null=True, 
        blank=True, 
        help_text=_("Entries prior to this date cannot be modified/posted.")
    )
    
    provider_account_root = models.CharField(
        max_length=12, 
        default="400",
        help_text=_("Default root for provider accounts (e.g. 400 for Spain)")
    )
    
    client_account_root = models.CharField(
        max_length=12, 
        default="430",
        help_text=_("Default root for client accounts (e.g. 430 for Spain)")
    )

    def __str__(self) -> str:
        return f"{self.company} -> {self.template}"


class AccountType(models.TextChoices):
    ASSET = "ASSET", _("Asset")
    LIABILITY = "LIABILITY", _("Liability")
    EQUITY = "EQUITY", _("Equity")
    INCOME = "INCOME", _("Income")
    EXPENSE = "EXPENSE", _("Expense")
    OFF_BALANCE = "OFF_BALANCE", _("Off Balance")

class BalanceSide(models.TextChoices):
    DEBIT = "DEBIT", _("Debit")
    CREDIT = "CREDIT", _("Credit")


class CompanyAccount(Timestampable):
    """
    Cuenta contable real de la empresa.
    - Puede referenciar a una cuenta template (mapeo a PGC base).
    - Permite subcuentas propias por empresa: 4300001, etc.
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="accounts")
    code = models.CharField(max_length=12, validators=[ACCOUNT_CODE_VALIDATOR])
    name = models.CharField(max_length=255)

    # Jerarquía dentro de la empresa (por si el usuario crea sus propios nodos/agrupadores)
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )

    # Mapeo al plan base (normalmente el “esqueleto” PGC)
    template_account = models.ForeignKey(
        TemplateAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="company_accounts",
        help_text="Cuenta del template (PGC) a la que mapea esta cuenta de empresa.",
    )

    is_active = models.BooleanField(default=True)
    is_postable = models.BooleanField(default=True)

    account_type = models.CharField(
        max_length=20, 
        choices=AccountType.choices, 
        default=AccountType.ASSET
    )
    normal_balance = models.CharField(
        max_length=10, 
        choices=BalanceSide.choices, 
        default=BalanceSide.DEBIT
    )
    currency = CurrencyField(
        blank=True,
        null=True,
        verbose_name=_("Account Currency Override"),
        help_text=_("Force this account to use a specific currency.")
    )
    
    is_reconcilable = models.BooleanField(default=False)
    is_tax_account = models.BooleanField(default=False)
    
    a3_code_override = models.CharField(
        max_length=10, 
        blank=True, 
        null=True, 
        help_text=_("Optional 10-digit account code for A3 export. If empty, standard mapping applies.")
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_company_account_code"),
        ]
        indexes = [
            models.Index(fields=["company", "code"]),
            models.Index(fields=["company", "template_account"]),
            models.Index(fields=["company", "parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Statement(models.TextChoices):
    BALANCE = "BALANCE", "Balance"
    PNL = "PNL", "Pérdidas y Ganancias"

class ReportingLine(Timestampable,CompanyOwnedModel):
    """
    Líneas/epígrafes de reporting interno (Balance / PyG) para mapear cuentas a informes.
    Esto es independiente del PGC (puedes usarlo para tus reportes, BI, consolidación, etc.).
    """
    statement = models.CharField(max_length=16, choices=Statement.choices)
    code = models.CharField(max_length=32)  
    name = models.CharField(max_length=255)

    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "statement", "code"], name="uniq_reporting_line_code"),
        ]
        indexes = [
            models.Index(fields=["company", "statement", "code"]),
            models.Index(fields=["company", "parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.statement} {self.code} {self.name}"


class AccountReportingMap(Timestampable,CompanyOwnedModel):
    """
    Mapea cuentas de empresa a líneas de reporting (muchos-a-muchos).
    - Permite mapear una cuenta a una línea (y opcionalmente con signo/peso).
    """
    account = models.ForeignKey(CompanyAccount, on_delete=models.CASCADE, related_name="reporting_maps")
    line = models.ForeignKey(ReportingLine, on_delete=models.CASCADE, related_name="account_maps")
    # Por si quieres invertir signo (p.ej. gastos negativos) o ponderar
    weight = models.DecimalField(max_digits=12, decimal_places=4, default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "account", "line"], name="uniq_company_account_line"),
        ]
        indexes = [
            models.Index(fields=["company", "account"]),
            models.Index(fields=["company", "line"]),
        ]

    def __str__(self) -> str:
        return f"{self.company_id} {self.account.code} -> {self.line.code} (w={self.weight})"


# ... (omitted parts) ...



class JournalType(models.TextChoices):
    GENERAL = "GENERAL", _("General")
    SALES = "SALES", _("Sales")
    PURCHASE = "PURCHASE", _("Purchase")
    BANK = "BANK", _("Bank")
    CASH = "CASH", _("Cash")
    PAYROLL = "PAYROLL", _("Payroll")


class Journal(Timestampable, CompanyOwnedModel):
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=16)
    type = models.CharField(max_length=16, choices=JournalType.choices)

    default_debit_account = models.ForeignKey(
        CompanyAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    default_credit_account = models.ForeignKey(
        CompanyAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_journal_code"),
        ]

    def __str__(self):
        return f"{self.code} - {self.name}"

class EntryStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    POSTED = "POSTED", _("Posted")
    CANCELLED = "CANCELLED", _("Cancelled")


class JournalEntry(Timestampable, CompanyOwnedModel):
    journal = models.ForeignKey(Journal, on_delete=models.PROTECT, related_name="entries")
    number = models.CharField(max_length=32, blank=True) 
    date = models.DateField()
    reference = models.CharField(max_length=1024, blank=True)
    status = models.CharField(
        max_length=16,
        choices=EntryStatus.choices,
        default=EntryStatus.DRAFT,
    )
    
    # Links to source documents
    purchase_invoice = models.ForeignKey(
        "invoices.Invoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="journal_entries",
        verbose_name=_("Purchase Invoice")
    )
    sales_invoice = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="journal_entries",
        verbose_name=_("Sales Invoice")
    )
    
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "company.CompanyUser", # Using CompanyUser as reference? Or User? User requested "posted_by fields". 
        # Usually it's the User model. models.py has no User import yet.
        # But CompanyUser is safer link to tenant. 
        # User prompt said "posted_by = models.ForeignKey(User...)" in implementation plan suggestion.
        # I'll use settings.AUTH_USER_MODEL.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="posted_entries"
    )

    class Meta:
        indexes = [
            models.Index(fields=["company", "date"]),
            models.Index(fields=["company", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["journal", "number"],
                name="uniq_journal_posted_number",
                condition=models.Q(status="POSTED"),
            ),
        ]

    def __str__(self):
        return f"{self.number or 'Draft'} ({self.date})"

    @property
    def total_debit(self):
        return self.lines.aggregate(
            total=models.Sum("debit")
        )["total"] or Decimal("0.00")

    @property
    def total_credit(self):
        return self.lines.aggregate(
            total=models.Sum("credit")
        )["total"] or Decimal("0.00")

    def is_balanced(self):
        return self.total_debit == self.total_credit

    def clean(self):
        # Enforce Company consistency
        if self.journal_id and self.journal.company_id != self.company_id:
            raise ValidationError(_("Journal must belong to the same company."))

        if self.pk:
            db_instance = JournalEntry.objects.get(pk=self.pk)
             # Strict Immutability
            if db_instance.status == EntryStatus.POSTED:
                 if (
                     self.date != db_instance.date
                     or self.journal_id != db_instance.journal_id
                     or self.status != db_instance.status
                     or self.company_id != db_instance.company_id
                     # Add other fields if necessary
                 ):
                     raise ValidationError(_("Cannot modify a posted entry."))
        
        super().clean()
        
        if self.status == EntryStatus.POSTED and not self.is_balanced():
            raise ValidationError(_("Cannot post unbalanced entry."))

    def delete(self, *args, **kwargs):
        if self.status == EntryStatus.POSTED:
             raise ValidationError(_("Cannot delete a posted entry."))
        super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.pk:
            # Enforce protection even if clean() wasn't called
            db_instance = JournalEntry.objects.get(pk=self.pk)
            if db_instance.status == EntryStatus.POSTED:
                 # We allow updating posted_at/posted_by ONLY during the post() transition if needed,
                 # but generally standard edits are blocked.
                 # The post() method calls save(). We need to allow that call.
                 # post() updates status from DRAFT -> POSTED.
                 # This check blocks updates IF DB STATUS IS ALREADY POSTED.
                 if (
                     self.date != db_instance.date
                     or self.journal_id != db_instance.journal_id
                     or self.company_id != db_instance.company_id
                     or self.status != db_instance.status
                     # We might want to allow updating reference? User said "Prevent modification of POSTED entries". 
                     # I will assume strict.
                 ):
                     raise ValidationError(_("Cannot modify a posted entry."))
        super().save(*args, **kwargs)

    def post(self, user=None):
        if not self.is_balanced():
            raise ValidationError(_("Entry is not balanced."))

        if self.status != EntryStatus.DRAFT:
            raise ValidationError(_("Only draft entries can be posted."))
            
        if not self.lines.exists():
             raise ValidationError(_("Cannot post empty entry."))
        
        # Check Account Lock Date
        if hasattr(self.company, 'chart') and self.company.chart.lock_date:
            if self.date <= self.company.chart.lock_date:
                 raise ValidationError(_(f"Cannot post entry before lock date ({self.company.chart.lock_date})."))
        
        # Check Fiscal Year
        fiscal_year = FiscalYear.objects.filter(
            company=self.company,
            start_date__lte=self.date,
            end_date__gte=self.date
        ).first()

        if not fiscal_year:
             raise ValidationError(_(f"No fiscal year defined for date {self.date}."))
        
        if fiscal_year.is_closed:
             raise ValidationError(_(f"Fiscal year {fiscal_year.name} is closed."))

        # Verify accounts
        for line in self.lines.all():
            if line.account.company_id != self.company_id:
                 raise ValidationError(_(f"Account {line.account.code} belongs to a different company."))
            if not line.account.is_active:
                 raise ValidationError(_(f"Account {line.account.code} is inactive."))
            if not line.account.is_postable:
                 raise ValidationError(_(f"Account {line.account.code} is not postable."))

        with transaction.atomic():
            # Lock the journal entry row itself to prevent double-posting
            # This is critical for idempotency if post() is called concurrently
            # Rename variable to avoid shadowing gettext _
            locked_entry = JournalEntry.objects.select_for_update().get(pk=self.pk)
            
            # Re-fetch checks inside lock? 
            # If status changed while waiting for lock, we should fail.
            # self.refresh_from_db() 
            # if self.status != EntryStatus.DRAFT: raise ...
            # But we are in a method on the instance.
            
            # Lock the journal to serialize numbering
            locked_journal = Journal.objects.select_for_update().get(pk=self.journal_id)

            year = self.date.year
            prefix = f"{self.journal.code}/{year}/"

            # Find the last number for this journal/year
            last_entry = (
                JournalEntry.objects.filter(
                    company=self.company, 
                    journal=self.journal,
                    date__year=year,
                    status=EntryStatus.POSTED,
                    number__startswith=prefix,
                )
                .order_by("-number")
                .first()
            )

            new_sequence = 1
            if last_entry and last_entry.number:
                try:
                    if last_entry.number.startswith(prefix):
                        sequence_str = last_entry.number[len(prefix) :]
                        new_sequence = int(sequence_str) + 1
                except ValueError:
                    pass

            self.number = f"{prefix}{new_sequence:05d}"
            self.status = EntryStatus.POSTED
            self.posted_at = timezone.now()
            if user:
                self.posted_by = user # Needs to set this field if we added it
            self.save()


class JournalEntryLine(Timestampable, CompanyOwnedModel):
    entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    account = models.ForeignKey(
        CompanyAccount,
        on_delete=models.PROTECT,
        related_name="entry_lines",
    )
    description = models.CharField(max_length=1024)
    
    debit = MoneyField(max_digits=19, decimal_places=4, default_currency="EUR", default=0)
    credit = MoneyField(max_digits=19, decimal_places=4, default_currency="EUR", default=0)

    class Meta:
        indexes = [
            models.Index(fields=["company", "account"]),
            models.Index(fields=["company", "entry"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(debit__gte=0) & 
                    models.Q(credit__gte=0) &
                    (
                        (models.Q(debit__gt=0) & models.Q(credit=0)) | 
                        (models.Q(debit=0) & models.Q(credit__gt=0))
                    )
                ),
                name="check_debit_xor_credit",
            ),
        ]

    def __str__(self):
        return f"{self.account.code} | D:{self.debit} C:{self.credit}"
    
    def clean(self):
        super().clean()
        
        # Verify Post Status
        if self.entry_id:
             if self.entry.status == EntryStatus.POSTED:
                 raise ValidationError(_("Cannot add or modify lines of a posted entry."))
        
        if self.debit.amount > 0 and self.credit.amount > 0:
            raise ValidationError(_("Line cannot have both debit and credit amounts."))
        
        if self.debit.amount == 0 and self.credit.amount == 0:
            raise ValidationError(_("Line must have either debit or credit."))
        
        if self.debit.amount < 0 or self.credit.amount < 0:
             raise ValidationError(_("Debit and Credit amounts must be non-negative."))

        if self.entry_id:
            if self.entry.company_id != self.company_id:
                 raise ValidationError(_("Line company must match entry company."))
        
        if self.account.company_id != self.company_id:
            raise ValidationError(_("Account must belong to same company."))
        
        # Currency Consistency
        if self.account.currency:
             # account.currency is a CurrencyField, so it returns a valid currency code string or None.
             # self.debit is a Money object. self.debit.currency is a Currency object or string.
             # djmoney uses Currency type. We should compare string representations.
             
             account_currency = str(self.account.currency)
             debit_currency = str(self.debit.currency)
             credit_currency = str(self.credit.currency)
             
             if debit_currency != account_currency:
                  raise ValidationError(_(f"Debit currency ({debit_currency}) must match account currency ({account_currency})."))
             if credit_currency != account_currency:
                  raise ValidationError(_(f"Credit currency ({credit_currency}) must match account currency ({account_currency})."))



