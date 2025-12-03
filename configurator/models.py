from django.db import models
from djmoney.models.fields import MoneyField
from sales.models import Client
from timestamps.models import Timestampable
from materials.models import Material
from .utils.evaluator import safe_eval
from .validators import pricing_expression_validator

class ConfigurationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    COMPLETED = "completed", "Completed"
    CONVERTED = "converted", "Converted to Quote"

class Configuration(Timestampable):
    """
    Represents a configuration session (CPQ).
    Created before generating a Quote.
    """

    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=200, help_text="Internal label for this configuration")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    status = models.CharField(
        max_length=20,
        choices=ConfigurationStatus.choices,
        default=ConfigurationStatus.DRAFT,
    )

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
    )

    def __str__(self):
        return f"Config #{self.id} — {self.name or 'Unnamed'}"
    
    def get_pricing_context(self):
        """
        Builds the variables available to PricingRules.
        """

        items = self.items.all()

        material_cost = sum(
            float(i.material.cost_per_unit) * float(i.quantity)
            for i in items
        )

        quantity = sum(float(i.quantity) for i in items)

        item_count = items.count()

        base_total = sum(float(i.line_total.amount) for i in items)

        return {
            "material_cost": material_cost,
            "quantity": quantity,
            "item_count": item_count,
            "base_total": base_total,
        }

    def recalculate_price(self):
        """
        Evaluates all PricingRules and updates the total price.
        """
        context = self.get_pricing_context()

        total = context["base_total"]

        for rule in self.pricing_rules.all():
            try:
                adjustment = safe_eval(rule.expression, context)
                total += adjustment
            except Exception as exc:
                raise ValueError(
                    f"Error evaluating rule '{rule.name}': {exc}"
                )

        # Store as Money
        self.total_price = total
        self.save(update_fields=["total_price"])


class ConfigurationItem(Timestampable):
    """
    Materials or parts selected during configuration.
    Does NOT affect inventory or orders directly.
    """

    configuration = models.ForeignKey(
        Configuration,
        on_delete=models.CASCADE,
        related_name="items",
    )

    material = models.ForeignKey(
        "materials.Material",
        on_delete=models.PROTECT,
        help_text="Selectable component or part."
    )

    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)

    # Price chosen at configuration time (unit price)
    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
    )

    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        editable=False,
    )

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.material} x {self.quantity}"

class IncompatibilityRule(models.Model):
    """
    Material A is incompatible with Material B.
    """

    material_a = models.ForeignKey(
        Material,
        on_delete=models.CASCADE,
        related_name="incompatible_with",
    )
    material_b = models.ForeignKey(
        Material,
        on_delete=models.CASCADE,
        related_name="incompatible_with_reverse",
    )

    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("material_a", "material_b")

    def __str__(self):
        return f"{self.material_a} ❌ {self.material_b}"


class PricingRuleTemplate(models.Model):
    """
    A reusable pricing rule template that can be applied
    to many configurations.
    """

    name = models.CharField(max_length=100)

    expression = models.TextField(
        validators=[pricing_expression_validator],
        help_text=(
            "Reusable expression.\n"
            "Allowed variables:\n"
            "- base_total\n"
            "- material_cost\n"
            "- quantity\n"
            "- item_count\n"
            "Example: base_total * 1.10"
        )
    )

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.expression})"
    
class PricingRuleAssignment(models.Model):
    """
    Assigns a reusable PricingRuleTemplate to a configuration.
    Optional: allow override or enable/disable per configuration.
    """

    configuration = models.ForeignKey(
        Configuration,
        on_delete=models.CASCADE,
        related_name="pricing_rules"
    )

    rule = models.ForeignKey(
        PricingRuleTemplate,
        on_delete=models.CASCADE,
        related_name="assignments"
    )

    enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ("configuration", "rule")

    def __str__(self):
        return f"{self.configuration} → {self.rule}"