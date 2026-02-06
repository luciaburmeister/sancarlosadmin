from django.db import models
from timestamps.models import Timestampable
from djmoney.models.fields import MoneyField
from django.db.models.functions import Lower
from django.db.models import Index
from configurator.utils import validate_expression_for_safe_eval,safe_eval
from company.models import CompanyOwnedModel
from django.utils.translation import gettext_lazy as _
from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields import ArrayField

class Provider(Timestampable,CompanyOwnedModel):
    name = models.CharField(max_length=200, db_index=True, verbose_name=_("Name"))
    contact = models.CharField(max_length=200, blank=True,verbose_name=_("Contact"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Phone"))
    email = models.EmailField(blank=True,verbose_name=_("Email"))
    tax_identifier = models.CharField(max_length=100,blank=True,verbose_name=("Tax identifier"))

    def __str__(self):
        return self.name
    
    class Meta:
        ordering = [Lower("name")]  
        indexes = [
            Index(Lower("name"), name="provider_name_ci_idx")
        ]

class Category(Timestampable,CompanyOwnedModel):
    """
    E.g., 'Aluminum Profiles', 'Hardware', 'Paint', 'Labor'
    """
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

class UnitChoices(models.TextChoices):
    METERS = "m"
    UNITS = "u"
    KILOGRAMS = "kg"
    HOURS = "h"
    M2 = "m2"

class Material(Timestampable,CompanyOwnedModel):
    """
    Represents the database of items found in your 'MATERIALES' section.
    Matches Excel columns: REFERENCIA, PRECIO.
    """


    reference_code = models.CharField(max_length=50, unique=True, help_text="e.g., 4058, 4269", verbose_name=_("Reference code"))
    name = models.CharField(max_length=200, help_text="e.g., CERCO 50 mm, LAMA 200",db_index=True, verbose_name=_("Name"))
    category = models.ForeignKey(Category, on_delete=models.PROTECT, verbose_name=_("Category"))
    unit_type = models.CharField(max_length=2, choices=UnitChoices.choices, default='m', verbose_name=_("Unit type"))
    cost_per_unit = MoneyField(max_digits=10, decimal_places=4, help_text="Base cost from supplier", default_currency="EUR", verbose_name=_("Cost per unit"))
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, null=True, blank=False,verbose_name=_("Provider"))
    metadata = models.JSONField(default=dict)

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

    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name="components",
        verbose_name=_("Material")
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
        unique_together = ("material", "name")
        ordering = ["material__name", "name"]

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
    
    def compute_unit_price(self, context: dict | None = None):
        """
        Compute component per-unit price.

        - If context is provided → try ComponentPriceRule
        - Otherwise → fallback to static expression (material-based)
        """

        if context is not None:
            rules = self.price_rules.all()

            fallback = None

            for rule in rules:
                try:
                    if rule.is_fallback:
                        fallback = rule
                        continue

                    if safe_eval(rule.condition, context):
                        return rule.unit_price
                except Exception as e:
                    raise ValueError(
                        f"Pricing rule error on component '{self.name}': {e}"
                    )

            if fallback:
                return fallback.unit_price

        base = float(self.material.cost_per_unit.amount)
        component_context = {"base_cost": base, **self.metadata}

        value = safe_eval(self.unit_price_expression, component_context)

        return self.cost_per_unit.__class__(
            value,
            currency=self.cost_per_unit.currency,
        )

    
    def save(self, *args, **kwargs):
        """
        Automatically compute and store cost_per_unit whenever the component is saved.
        Uses a minimal context (only base_cost) because component-level prices usually
        do not depend on configuration-level parameters.
        """
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

    unit_price = MoneyField(
        max_digits=10,
        decimal_places=4,
        default_currency="EUR",
        verbose_name=_("Unit price"),
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
