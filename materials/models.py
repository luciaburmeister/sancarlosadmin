from django.db import models
from timestamps.models import Timestampable
from djmoney.models.fields import MoneyField
from django.db.models.functions import Lower
from django.db.models import Index
from configurator.utils import validate_expression_for_safe_eval,safe_eval
from company.models import CompanyOwnedModel

class Provider(Timestampable,CompanyOwnedModel):
    name = models.CharField(max_length=200, db_index=True)
    contact = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    tax_identifier = models.CharField(max_length=100,blank=True)

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
    name = models.CharField(max_length=100)
    
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


    reference_code = models.CharField(max_length=50, unique=True, help_text="e.g., 4058, 4269")
    name = models.CharField(max_length=200, help_text="e.g., CERCO 50 mm, LAMA 200",db_index=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    unit_type = models.CharField(max_length=2, choices=UnitChoices.choices, default='m')
    cost_per_unit = MoneyField(max_digits=10, decimal_places=4, help_text="Base cost from supplier", default_currency="EUR")
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, null=True, blank=False)
    metadata = models.JSONField(default=dict)

    def __str__(self):
        return f"[{self.reference_code}] {self.name}"


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
        related_name="components"
    )

    name = models.CharField(
        max_length=200,
        help_text="Human-friendly name, e.g., 'Left Rail', 'Right Rail'",
        db_index=True
    )

    code = models.CharField(
        max_length=50,
        blank=True,
        help_text="Optional component reference"
    )

    unit_price_expression = models.TextField(
        help_text="Enter an expression to calculate cost per material unit. Context available: base_cost",
        validators=[validate_expression_for_safe_eval]
    )

    cost_per_unit = MoneyField(max_digits=10, decimal_places=4, help_text="Base cost from supplier", default_currency="EUR",default=0)

    component_type = models.CharField(max_length=20, choices=ComponentType.choices, default=ComponentType.STANDARD, db_index=True,)

    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("material", "name")
        ordering = ["material__name", "name"]

    def __str__(self):
        return f"{self.material.reference_code} – {self.name}"
    
    def compute_unit_price(self, context: dict | None = None):
        """
        Compute component-specific per-unit cost.
        Only base_cost + component metadata are allowed.
        """
        base = float(self.material.cost_per_unit.amount)

        component_context = {"base_cost": base, **self.metadata}

        value = safe_eval(self.unit_price_expression, component_context)

        return self.cost_per_unit.__class__(
            value, currency=self.cost_per_unit.currency
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
