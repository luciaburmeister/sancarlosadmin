# accounting/models.py
from __future__ import annotations

from django.db import models, transaction
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
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


class AccountRole(models.TextChoices):
    """
    What the journal generators book to an account. At most one account per
    company, and per template, holds each role.
    """
    ROUNDING = "rounding", _("Rounding")
    GOODS_SALES = "goods_sales", _("Sales of goods")
    SERVICES_SALES = "services_sales", _("Sales of services")
    SALES_DISCOUNTS = "sales_discounts", _("Sales discounts")
    CASH = "cash", _("Cash")
    SUSPENSE = "suspense", _("Suspense (pending items)")


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
    role = models.CharField(
        max_length=20,
        choices=AccountRole.choices,
        blank=True,
        default="",
        verbose_name=_("Role"),
        help_text=_(
            "Given to the company's copy of this account when the chart is installed, "
            "unless the company already has an account with that role."
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["template", "code"], name="uniq_template_account_code"),
            models.UniqueConstraint(
                fields=["template", "role"],
                condition=~models.Q(role=""),
                name="uniq_template_account_role",
            ),
            models.CheckConstraint(
                check=models.Q(role="") | models.Q(is_postable=True),
                name="template_account_role_postable",
            ),
        ]
        indexes = [
            models.Index(fields=["template", "code"]),
            models.Index(fields=["template", "parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def clean(self):
        super().clean()
        if self.role and not self.is_postable:
            raise ValidationError({"role": _("An account with a role must be active and postable.")})

    def save(self, *args, **kwargs):
        # Taking a role moves it off the account that held it
        with transaction.atomic():
            if self.role:
                TemplateAccount.objects.filter(
                    template_id=self.template_id, role=self.role,
                ).exclude(pk=self.pk).update(role="")
            super().save(*args, **kwargs)


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

    ledger_start_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Ledger start date"),
        help_text=_(
            "Invoices and payments dated earlier stay out of the ledger: their balances "
            "come in with the opening entry, dated this day."
        ),
    )

    bank_account_root = models.CharField(
        max_length=12,
        default="572",
        help_text=_("Default root for the company's bank accounts (e.g. 572 for Spain)")
    )

    notes_payable_account_code = models.CharField(
        max_length=12,
        default="401",
        help_text=_("Code of the account issued promissory notes settle to until their due date (e.g. 401 for Spain)")
    )

    notes_receivable_account_code = models.CharField(
        max_length=12,
        default="431",
        help_text=_("Code of the account received promissory notes settle to until their due date (e.g. 431 for Spain)")
    )

    partner_account_length = models.PositiveSmallIntegerField(
        default=7,
        validators=[MinValueValidator(4), MaxValueValidator(12)],
        verbose_name=_("Client/provider account length"),
        help_text=_(
            "Total digits of the accounts created for clients and providers: the root "
            "followed by a consecutive number per company (e.g. 8 → 43000001)."
        ),
    )

    rounding_tolerance = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.05"),
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_("Rounding tolerance"),
        help_text=_("Largest difference absorbed as rounding. Bigger gaps are left for manual review."),
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(rounding_tolerance__gte=0),
                name="chart_rounding_tolerance_non_negative",
            ),
            models.CheckConstraint(
                check=models.Q(partner_account_length__gte=4, partner_account_length__lte=12),
                name="chart_partner_account_length_range",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        for field in ("provider_account_root", "client_account_root", "bank_account_root"):
            root = getattr(self, field) or ""
            if not root.isdigit():
                errors[field] = _("The root must be numeric.")
            elif self.partner_account_length and len(root) >= self.partner_account_length:
                errors[field] = _("The root must be shorter than the client/provider account length.")
        if self.pk:
            stored = CompanyChartOfAccounts.objects.filter(pk=self.pk).values_list("ledger_start_date", flat=True).first()
            opened = JournalEntry.objects.filter(
                company_id=self.company_id, is_opening=True, status=EntryStatus.POSTED,
            ).exists()
            if stored != self.ledger_start_date and opened:
                errors["ledger_start_date"] = _("The opening entry is posted on this date: reverse it before moving it.")
        if errors:
            raise ValidationError(errors)

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
    role = models.CharField(
        max_length=20,
        choices=AccountRole.choices,
        blank=True,
        default="",
        verbose_name=_("Role"),
        help_text=_(
            "What the automatic journal entries book here: rounding differences, income "
            "from sales of goods or of services, the discounts on sales invoices, cash "
            "payments, or what the opening entry leaves pending. One account per role per company."
        ),
    )

    a3_code_override = models.CharField(
        max_length=10, 
        blank=True, 
        null=True, 
        help_text=_("Optional 10-digit account code for A3 export. If empty, standard mapping applies.")
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_company_account_code"),
            models.UniqueConstraint(
                fields=["company", "role"],
                condition=~models.Q(role=""),
                name="uniq_company_account_role",
            ),
            models.CheckConstraint(
                check=models.Q(role="") | models.Q(is_postable=True, is_active=True),
                name="account_role_postable_active",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "code"]),
            models.Index(fields=["company", "template_account"]),
            models.Index(fields=["company", "parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def clean(self):
        super().clean()
        if self.role and not (self.is_postable and self.is_active):
            raise ValidationError({"role": _("An account with a role must be active and postable.")})

    def save(self, *args, **kwargs):
        # Taking a role moves it off the account that held it
        with transaction.atomic():
            if self.role:
                CompanyAccount.objects.filter(
                    company_id=self.company_id, role=self.role,
                ).exclude(pk=self.pk).update(role="")
            super().save(*args, **kwargs)

    @classmethod
    def for_role(cls, company_id, role):
        return cls.objects.filter(company_id=company_id, role=role).first()


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


class CostCenter(Timestampable, CompanyOwnedModel):
    """
    Centro de coste: dimensión analítica del gasto, independiente del PGC.

    Los centros forman un árbol por empresa. Cada documento (pedido de compra,
    factura recibida) apunta a UN solo centro, y los informes agregan hacia
    arriba por el árbol — así no hay que repartir importes por porcentajes.

    A diferencia de CompanyAccount, cuya jerarquía va implícita en el prefijo
    numérico del código, aquí el código lo elige el usuario, así que la
    jerarquía se recorre por `parent`.
    """
    code = models.CharField(max_length=32, verbose_name=_("Code"))
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("Parent"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    is_assignable = models.BooleanField(
        default=True,
        verbose_name=_("Assignable"),
        help_text=_("Uncheck for grouping nodes that only aggregate their children."),
    )

    class Meta:
        verbose_name = _("Cost Center")
        verbose_name_plural = _("Cost Centers")
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"],
                name="uniq_cost_center_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "code"]),
            models.Index(fields=["company", "parent"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def ancestors(self):
        """Root-first list of ancestors, excluding self. Stops on a cycle."""
        chain = []
        seen = {self.pk}
        node = self.parent
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    @property
    def full_name(self) -> str:
        """e.g. 'Operaciones / Flota / Flota Cuba'."""
        return " / ".join([node.name for node in self.ancestors()] + [self.name])

    def descendant_ids(self, include_self=True):
        """
        Every id in this subtree — the set to filter spend by when rolling a
        report up to this node.
        """
        ids = {self.pk} if include_self else set()
        frontier = [self.pk]
        while frontier:
            children = list(
                CostCenter.objects
                .filter(parent_id__in=frontier)
                .exclude(pk__in=ids)
                .values_list("pk", flat=True)
            )
            if not children:
                break
            ids.update(children)
            frontier = children
        return ids

    def clean(self):
        super().clean()
        if not self.parent_id:
            return
        if self.pk and self.parent_id == self.pk:
            raise ValidationError({"parent": _("A cost center cannot be its own parent.")})
        if self.company_id and self.parent.company_id != self.company_id:
            raise ValidationError({"parent": _("Parent must belong to the same company.")})
        # Walk up from the proposed parent; hitting self means a cycle.
        seen = set()
        node = self.parent
        while node is not None:
            if node.pk == self.pk:
                raise ValidationError({"parent": _("This parent would create a cycle.")})
            if node.pk in seen:
                break
            seen.add(node.pk)
            node = node.parent


# ... (omitted parts) ...


class IncomeType(models.TextChoices):
    """Which sales account a business unit's income is booked to (PGC 700 / 705)."""
    GOODS = "goods", _("Goods")
    SERVICES = "services", _("Services")


class BusinessUnit(Timestampable, CompanyOwnedModel):
    """
    Unidad de negocio: what a product or inventory item sells, goods or
    services, and so which sales account its invoice lines are booked to.

    Products and inventory items point to one; an invoice line takes its
    item's unit when created, and the company's default unit when there is
    nothing behind it (manual lines, advances, imports).
    """
    code = models.CharField(
        max_length=32,
        blank=True,
        verbose_name=_("Code"),
        help_text=_("Assigned automatically, numbered per company (UN-001, UN-002, ...)."),
    )
    name = models.CharField(max_length=255, verbose_name=_("Name"))
    income_type = models.CharField(
        max_length=10,
        choices=IncomeType.choices,
        default=IncomeType.GOODS,
        verbose_name=_("Goods or services"),
        help_text=_("Books its sales to sales of goods or of services. Fixed once a posted entry uses it."),
    )
    is_default = models.BooleanField(
        default=False,
        verbose_name=_("Default"),
        help_text=_("Used for invoice lines with no product or item behind them. One per company."),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Business Unit")
        verbose_name_plural = _("Business Units")
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_business_unit_company_code"),
            models.UniqueConstraint(
                fields=["company"],
                condition=models.Q(is_default=True),
                name="uniq_business_unit_company_default",
            ),
            models.CheckConstraint(
                check=models.Q(is_default=False) | models.Q(is_active=True),
                name="business_unit_default_active",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @classmethod
    def default_for(cls, company_id):
        return cls.objects.filter(company_id=company_id, is_default=True).first()

    def has_posted_sales(self):
        """Lines of it (or deducting an advance of it) are in a posted, unreversed sales entry."""
        from invoices.models import SalesInvoiceItem

        return SalesInvoiceItem.objects.filter(
            models.Q(business_unit=self) | models.Q(advance_line__business_unit=self),
            invoice__journal_entries__status=EntryStatus.POSTED,
            invoice__journal_entries__reversal_of__isnull=True,
            invoice__journal_entries__reversal__isnull=True,
        ).exists()

    def clean(self):
        super().clean()
        if self.is_default and not self.is_active:
            raise ValidationError({"is_default": _("The default business unit must be active.")})
        if self._income_type_changed() and self.has_posted_sales():
            raise ValidationError({"income_type": _("Posted entries already book this unit's sales: it can no longer change.")})

    def _income_type_changed(self):
        if not self.pk:
            return False
        stored = BusinessUnit.objects.filter(pk=self.pk).values_list("income_type", flat=True).first()
        return stored is not None and stored != self.income_type

    CODE_PREFIX = "UN"

    def save(self, *args, **kwargs):
        from configurator.utils.codegen import next_scoped_code

        with transaction.atomic():
            if not self.code:
                self.code = next_scoped_code(
                    model=BusinessUnit,
                    scope_filter={"company_id": self.company_id},
                    prefix=self.CODE_PREFIX,
                    width=3,
                )
            if self._income_type_changed() and self.has_posted_sales():
                raise ValidationError(_("Posted entries already book this unit's sales: it can no longer change."))
            
            if self.is_default:
                BusinessUnit.objects.filter(
                    company_id=self.company_id, is_default=True,
                ).exclude(pk=self.pk).update(is_default=False)
            super().save(*args, **kwargs)


class AllocationAccountRule(Timestampable, CompanyOwnedModel):
    """
    Where a payment allocated to a kind of document books its counterpart,
    for documents with no ledger strategy in code (e.g. expense claims →
    465). An account set on the allocation itself takes precedence.
    """
    content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.CASCADE,
        verbose_name=_("Document type"),
    )
    account = models.ForeignKey(
        CompanyAccount,
        on_delete=models.PROTECT,
        related_name="allocation_rules",
        verbose_name=_("Account"),
    )

    class Meta:
        verbose_name = _("Payment account rule")
        verbose_name_plural = _("Payment account rules")
        constraints = [
            models.UniqueConstraint(fields=["company", "content_type"], name="uniq_allocation_rule_document_type"),
        ]

    def __str__(self):
        return f"{self.content_type} → {self.account}"

    def clean(self):
        super().clean()
        if self.account_id and self.company_id and self.account.company_id != self.company_id:
            raise ValidationError({"account": _("The account belongs to another company.")})


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
    payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_entries",
        verbose_name=_("Payment"),
        help_text=_("A journalized payment can't be deleted: cancel it, and its entry is reversed."),
    )
    is_opening = models.BooleanField(
        default=False,
        verbose_name=_("Opening entry"),
        help_text=_("The balances the ledger starts from, on the chart's ledger start date. One per company."),
    )
    reversal_of = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reversal",
        verbose_name=_("Reverses"),
        help_text=_("The posted entry this one cancels, debits and credits swapped."),
    )

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        "company.CompanyUser", 
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
            models.UniqueConstraint(
                fields=["company"],
                name="uniq_company_opening_entry",
                condition=models.Q(is_opening=True),
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

    def delete(self, *args, allow_posted=False, **kwargs):

        if self.status == EntryStatus.POSTED and not allow_posted:
             raise ValidationError(_("Cannot delete a posted entry."))
        return super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.pk:
            # Enforce protection even if clean() wasn't called
            db_instance = JournalEntry.objects.get(pk=self.pk)
            if db_instance.status == EntryStatus.POSTED:

                 if (
                     self.date != db_instance.date
                     or self.journal_id != db_instance.journal_id
                     or self.company_id != db_instance.company_id
                     or self.status != db_instance.status

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
        verbose_name = _("Journal entry line")
        verbose_name_plural = _("Journal entry lines")
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



