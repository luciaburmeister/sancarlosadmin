from django.db import models
from timestamps.models import Timestampable
from djmoney.models.fields import MoneyField
from django.db.models.functions import Lower
from django.db.models import Index
from configurator.utils import validate_expression_for_safe_eval,safe_eval
from company.models import CompanyOwnedModel, BaseBankAccount
from django.utils.translation import gettext_lazy as _
from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from materials.validators import validate_rule_key

class PaymentMethod(models.TextChoices):
    TRANSFER = 'transfer', _('Bank Transfer')
    CASH = 'cash', _('Cash')
    CARD = 'card', _('Credit/Debit Card')
    CHEQUE = 'cheque', _('Cheque')
    SEPA_DIRECT_DEBIT = 'sepa_direct_debit', _('SEPA - Direct debit')
    CONFIRMING = 'confirming', _('Confirming')


class Provider(Timestampable,CompanyOwnedModel):
    name = models.CharField(max_length=200, db_index=True, verbose_name=_("Name"))
    contact = models.CharField(max_length=200, blank=True,verbose_name=_("Contact"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Phone"))
    email = models.EmailField(blank=True,verbose_name=_("Email"))
    tax_identifier = models.CharField(max_length=100,blank=True,verbose_name=("Tax identifier"))
    associated_emails = ArrayField(
        models.EmailField(),
        blank=True,
        default=list,
        verbose_name=_("Associated Emails"),
        help_text=_("List of email addresses from which invoices for this provider are received. Separate with commas.")
    )
    account = models.ForeignKey(
        'accounting.CompanyAccount',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="providers",
        verbose_name=_("Accounting Account")
    )
    default_cost_center = models.ForeignKey(
        'accounting.CostCenter',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="providers",
        verbose_name=_("Default Cost Center"),
        help_text=_("Automatically assigned to new purchase orders and invoices for this provider.")
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER,
        verbose_name=_("Payment Method"),
        help_text=_("Default payment method for this provider; copied to new purchase orders and can be overridden per order."),
    )
    payment_terms_days = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_("Payment Terms (Days)"),
        help_text=_("Default payment terms for this provider; copied to new purchase orders and can be overridden per order."),
    )
    def __str__(self):
        return self.name

    class Meta:
        ordering = [Lower("name")]
        indexes = [
            Index(Lower("name"), name="provider_name_ci_idx"),
            GinIndex(fields=['associated_emails'], name='provider_emails_gin_idx')
        ]
        permissions = [
            ("can_import_providers", _("Can import providers")),
        ]

    def clean(self):
        super().clean()
        self._validate_unique_tax_identifier()

    def save(self, *args, **kwargs):
        self._validate_unique_tax_identifier()
        super().save(*args, **kwargs)

    def _validate_unique_tax_identifier(self):
        # Blank tax identifiers are still allowed (e.g. providers created by
        # automated import flows before a CIF/NIF is known), so they are
        # excluded from the uniqueness check to avoid blocking those flows.
        if not self.tax_identifier or not self.company_id:
            return
        duplicate = Provider.objects.filter(
            company_id=self.company_id,
            tax_identifier__iexact=self.tax_identifier,
        ).exclude(pk=self.pk)
        if duplicate.exists():
            raise ValidationError({
                "tax_identifier": _(
                    "Another provider in this company already uses this tax identifier."
                )
            })

class ProviderBankAccount(CompanyOwnedModel, BaseBankAccount):
    provider = models.ForeignKey(
        Provider,
        on_delete=models.CASCADE,
        related_name="bank_accounts",
        verbose_name=_("Provider")
    )
    is_default = models.BooleanField(default=False, verbose_name=_("Is Default"))

    class Meta:
        verbose_name = _("Provider Bank Account")
        verbose_name_plural = _("Provider Bank Accounts")

    def __str__(self):
        return f"{self.bank_name} - {self.account_number or self.iban} ({self.provider})"

class Category(Timestampable,CompanyOwnedModel):
    """
    E.g., 'Aluminum Profiles', 'Hardware', 'Paint', 'Labor'
    """
    name = models.CharField(max_length=100, verbose_name=_("Name"))

    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name


class MaterialFamily(CompanyOwnedModel):
    """
    Product category / family grouping for a material.
    E.g., 'Perfilería', 'Herrajes', 'Vidrio'.
    """
    name = models.CharField(max_length=200, verbose_name=_("Name"))

    class Meta:
        verbose_name = _("Category")
        verbose_name_plural = _("Categories")
        unique_together = [("company", "name")]
        ordering = ["name"]

    def __str__(self):
        return self.name

class UnitChoices(models.TextChoices):
    METERS = "m"
    UNITS = "u"
    KILOGRAMS = "kg"
    HOURS = "h"
    M2 = "m2"
    TONELADAS = "t"
    M3 = "m3"


        
class Material(Timestampable,CompanyOwnedModel):
    """
    Represents the database of items found in your 'MATERIALES' section.
    Matches Excel columns: REFERENCIA, PRECIO.
    """


    reference_code = models.CharField(max_length=50, unique=True, help_text="e.g., 4058, 4269", verbose_name=_("Reference code"))
    rule_key = models.SlugField(
        max_length=50,
        help_text=_(
            "Internal identifier used in price rules. "
            "Must start with a letter and contain only letters, numbers, or underscores. "
            "Example: steel_4058"
        ),
        validators=[validate_rule_key],
        verbose_name=_("Rule key"),
        null=True
    )
    name = models.CharField(max_length=200, help_text="e.g., CERCO 50 mm, LAMA 200",db_index=True, verbose_name=_("Name"))
    category = models.ForeignKey(Category, on_delete=models.PROTECT, verbose_name=_("Category"))
    family = models.ForeignKey(
        MaterialFamily,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Category"),
    )
    unit_type = models.CharField(max_length=2, choices=UnitChoices.choices, default='m', verbose_name=_("Unit type"))
    cost_per_unit = MoneyField(max_digits=10, decimal_places=4, help_text="Base cost from supplier", default_currency="EUR", verbose_name=_("Cost per unit"))
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, null=True, blank=False,verbose_name=_("Provider"))
    metadata = models.JSONField(default=dict)

    class Meta:
        unique_together = (
            ("rule_key", "company"),
        )
        permissions = [
            ("can_import_materials", _("Can import materials")),
        ]

    def __str__(self):
        return f"[{self.reference_code}] {self.name}"
    
    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        recompute = (
            update_fields is None or "cost_per_unit" in update_fields
        )

        with transaction.atomic():
            super().save(*args, **kwargs)

            if recompute:
                for component in self.components.all():
                    component.save(update_fields=["cost_per_unit", "updated_at"])



class ComponentType(models.TextChoices):
    STANDARD = "STANDARD", "Standard"
    ADD_ON = "ADD_ON", "Add On"
    

class Component(Timestampable,CompanyOwnedModel):
    """
    A re-usable building block derived from a Material.
    Allows multiple components per material with custom logic,
    names, formulas, and configuration behavior.
    """

    materials = models.ManyToManyField(
        Material,
        related_name="components",
        verbose_name=_("Materials"),
        blank=True
    )

    name = models.CharField(
        max_length=200,
        help_text="Human-friendly name, e.g., 'Left Rail', 'Right Rail'",
        db_index=True,
        verbose_name=_("Name")
    )

    code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional component reference",
        verbose_name=_("Code")
    )

    unit_price_expression = models.TextField(
        help_text="Enter an expression to calculate cost per material unit. Context available: base_cost",
        validators=[validate_expression_for_safe_eval]
    )

    cost_per_unit = MoneyField(max_digits=10, decimal_places=4, help_text="Base cost from supplier", default_currency="EUR",default=0,
                               verbose_name=_("Cost per unit"))

    component_type = models.CharField(max_length=20, choices=ComponentType.choices, default=ComponentType.STANDARD, db_index=True,
                                      verbose_name=_("Component Type"))

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Production stages this component participates in"),
        verbose_name=_("Production Stages"),
    )

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name}"

    def clean(self):
        super().clean()
        if self.production_stages and self.company:
            company_stages = set(self.company.production_stages or [])
            component_stages = set(self.production_stages)

            if not component_stages.issubset(company_stages):
                raise ValidationError(
                    {
                        "production_stages": _(
                            "Component stages must be a subset of the company production stages."
                        )
                    }
                )
    
    def get_pricing_context(self) -> dict:
        """
        Build the pricing context from the component's internal state (materials, metadata).
        Returns a dictionary with 'prices' and 'base_cost'.
        """
        if self.pk:
            materials = self.materials.all()
            prices = {
                m.rule_key: float(m.cost_per_unit.amount)
                for m in materials
            }
        else:
            prices = {}
        
        base_cost = sum(prices.values())
        
        return {
            "prices": prices,
            "base_cost": base_cost, 
            **self.metadata
        }

    def to_money(self, val):
        return self.cost_per_unit.__class__(
            val,
            currency=self.cost_per_unit.currency
        )

    def update_price_rules(self):
        """
        Calculates and updates the unit_price for all ComponentPriceRule objects
        associated with this component based on the current material costs.
        This ignores configuration-specific context.
        """
        if not self.pk:
            return

        eval_context = self.get_pricing_context()
        rules = self.price_rules.all()

        for rule in rules:
            try:
                rule.unit_price = self.to_money(rule.calculate_price(eval_context))
            except Exception:
                pass
        
        ComponentPriceRule.objects.bulk_update(rules, ["unit_price"])

    def compute_unit_price(self, context: dict | None = None):
        """
        Compute component per-unit price using cached rule prices.
        
        1. Get base component context.
        2. Merge with provided external context.
        3. Check Rules (using cached unit_price).
        4. Fallback to Expression or Base Cost.
        """
        
        # 1. Start with internal context
        eval_context = self.get_pricing_context()
        
        # 2. Merge with external context
        if context:
            eval_context.update(context)

        # 3. Check Price Rules
        if self.pk:
            rules = self.price_rules.all()
        else:
            rules = []

        for rule in rules:
            if rule.applies_to(eval_context):
                return rule.unit_price

        if self.unit_price_expression:
            try:
                value = safe_eval(self.unit_price_expression, eval_context)
                return self.to_money(value)
            except Exception:
                 pass

        return self.to_money(eval_context.get("base_cost", 0))

    
    def save(self, *args, **kwargs):
        """
        Automatically compute and store cost_per_unit whenever the component is saved.
        - First updates all rule prices based on current material costs.
        - Then computes the cost_per_unit (base/default price).
        """
        self.update_price_rules()
        computed_price = self.compute_unit_price(context=None)
        self.cost_per_unit = computed_price
        super().save(*args, **kwargs)


class ComponentPriceRule(models.Model):
    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        related_name="price_rules",
        verbose_name=_("Component"),
    )

    condition = models.TextField(
        help_text=_(
            "Boolean expression evaluated with safe_eval.\n"
            "Must return True or False.\n"
            "Example: width <= 1000 and quantity >= 10"
        )
    )

    unit_price_expression = models.TextField(
        help_text=_("Enter an expression to calculate cost. Context: base_cost (if fallback) or material prices."),
        validators=[validate_expression_for_safe_eval],
        blank=True,
        null=True
    )

    unit_price = MoneyField(
        max_digits=10,
        decimal_places=4,
        default_currency="EUR",
        verbose_name=_("Unit price"),
        blank=True
    )

    priority = models.PositiveIntegerField(
        default=0,
        help_text=_("Lower values are evaluated first"),
        db_index=True
    )

    is_fallback = models.BooleanField(
        default=False,
        help_text=_("Used if no other rule matches"),
    )

    class Meta:
        ordering = ["priority"]

    def __str__(self):
        return f"{self.component.name} → {self.unit_price}"

    def clean(self):

        if self.is_fallback and self.condition.strip() != "True":
            raise ValidationError(
                {"condition": "Fallback rule condition must be exactly 'True'."}
            )

        try:
            validate_expression_for_safe_eval(self.condition)
        except ValueError as e:
            raise ValidationError({"condition": str(e)})
            
    def calculate_price(self, context: dict) -> float:
        """
        Calculate the price for this rule given the context.
        Prioritizes unit_price_expression over unit_price.
        """
        if self.unit_price_expression:
            return float(safe_eval(self.unit_price_expression, context))
        return float(self.unit_price.amount)

    def applies_to(self, context: dict) -> bool:
        """
        Check if this rule applies given the context.
        Returns False if condition evaluates to False or fails (e.g. missing vars).
        """
        try:
            return safe_eval(self.condition, context)
        except Exception:
            return False

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.component.save()

    def delete(self, *args, **kwargs):
        component = self.component
        super().delete(*args, **kwargs)
        # Recompute component price after rule deletion
        component.refresh_from_db()
        component.save()


class PriceSheet(Timestampable, CompanyOwnedModel):
    """
    A named, reusable pricing list.
    Users pick materials and/or components to include;
    the exported price is always the live cost_per_unit.
    """
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Price Sheet")
        verbose_name_plural = _("Price Sheets")
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class PriceSheetItem(models.Model):
    """
    A single line in a PriceSheet.
    Exactly one of `material` or `component` must be set.
    """
    price_sheet = models.ForeignKey(
        PriceSheet,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Price Sheet"),
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Material"),
    )
    component = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Component"),
    )
    display_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Display Name"),
        help_text=_("Optional public name. If blank, the material/component name is used."),
    )
    order = models.PositiveIntegerField(default=0, verbose_name=_("Order"))

    class Meta:
        verbose_name = _("Price Sheet Item")
        verbose_name_plural = _("Price Sheet Items")
        ordering = ["order", "pk"]

    def clean(self):
        super().clean()
        if not self.material and not self.component:
            raise ValidationError(
                _("You must select either a material or a component.")
            )
        if self.material and self.component:
            raise ValidationError(
                _("Select only one: a material or a component, not both.")
            )

    @property
    def resolved_name(self):
        if self.display_name:
            return self.display_name
        if self.material:
            return self.material.name
        return self.component.name if self.component else ""

    @property
    def resolved_reference(self):
        if self.material:
            return self.material.reference_code
        return self.component.code if self.component else ""

    @property
    def resolved_unit_price(self):
        if self.material:
            return self.material.cost_per_unit
        return self.component.cost_per_unit if self.component else None

    @property
    def resolved_unit_type(self):
        if self.material:
            return self.material.get_unit_type_display()
        return "-"

    @property
    def resolved_provider(self):
        if self.material and self.material.provider:
            return self.material.provider.name
        return ""

    @property
    def resolved_category(self):
        if self.material and self.material.category:
            return self.material.category.name
        return ""

    @property
    def item_type(self):
        return _("Material") if self.material else _("Component")

    def __str__(self):
        return self.resolved_name
