from django.db import models
from djmoney.models.fields import MoneyField
from sales.models import Client
from timestamps.models import Timestampable
from materials.models import Material
from configurator.utils import safe_eval
from .validators import pricing_expression_validator

class ConfigurationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    COMPLETED = "completed", "Completed"
    CONVERTED = "converted", "Converted to Quote"

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


    
class ProductTemplate(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="products/", blank=True, null=True)

    def __str__(self):
        return self.name

class ParameterType(models.TextChoices):
    NUMBER = "number"
    TEXT = "text"
    BOOLEAN = "boolean"
    SELECT = "select"


class ProductParameter(models.Model):
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="parameters"
    )

    name = models.CharField(max_length=100)     
    label = models.CharField(max_length=100)  
    type = models.CharField(
        max_length=10,
        choices=ParameterType.choices,
        default=ParameterType.NUMBER,
    )
    options = models.JSONField(blank=True, null=True, default=list)
    required = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.product} – {self.label}"

    def normalize_value(self, raw_value):
        """
        Convert raw string value into the correct Python type
        based on the parameter type.
        """
        if self.type == ParameterType.NUMBER:
            try:
                return float(raw_value)
            except (TypeError, ValueError):
                return raw_value

        if self.type == ParameterType.BOOLEAN:
            if isinstance(raw_value, str):
                return raw_value.lower() == "true"
            return bool(raw_value)

        # TEXT or SELECT
        return raw_value

class BOMRule(models.Model):
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="bom_rules"
    )

    material = models.ForeignKey(Material, on_delete=models.PROTECT)

    quantity_expression = models.TextField(
        help_text="Expression evaluated with safe_eval"
    )

    length_expression = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.product} → {self.material}"


class PricingRuleTemplate(models.Model):
    """
    A reusable pricing rule template that can be applied
    to many configurations.
    """
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="pricing_rules",
        null=True,
        blank=False
    )

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
    


class Configuration(Timestampable):
    """
    Represents a configuration session (CPQ).
    Created before generating a Quote.
    """

    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=200, help_text="Internal label for this configuration")
    product = models.ForeignKey(
    ProductTemplate,
    on_delete=models.PROTECT,
    related_name="configurations",
    null=True,
    blank=False
    )

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
        Builds the full evaluation context used for:
        - Pricing rules
        - Computed product variables
        - BOM expressions
        """

        # 1️⃣ Load user parameters
        context = {
            pv.parameter.name: pv.parameter.normalize_value(pv.value)
            for pv in self.param_values.select_related("parameter")
        }

        # 2️⃣ Basic aggregates (always needed)
        items = self.items.all()

        context.update({
            "material_cost": sum(float(i.material.cost_per_unit.amount) * float(i.quantity) for i in items),
            "quantity": sum(float(i.quantity) for i in items),
            "item_count": items.count(),
            "base_total": sum(float(i.line_total.amount) for i in items),
        })

        # 3️⃣ 🔥 Evaluate product's computed variables (constants + expressions)
        context = self.evaluate_computed_variables(context)

        return context


    def recalculate_price(self):
        """
        Evaluates all PricingRules and updates the total price.
        """
        context = self.get_pricing_context()

        total = context["base_total"]

        for rule_assignmanet in self.pricing_rules.select_related('rule').filter(enabled=True):
            try:
                adjustment = safe_eval(rule_assignmanet.rule.expression, context)
                total += adjustment
            except Exception as exc:
                raise ValueError(
                    f"Error evaluating rule '{rule_assignmanet}': {exc}"
                )

        self.total_price = total
        self.save(update_fields=["total_price"])

    def generate_bom(self):
        # Track which items were created this cycle
        processed = set()

        # Build context including computed variables
        context = self.get_pricing_context()

        # Generate BOM items
        for rule in self.product.bom_rules.all():
            qty = safe_eval(rule.quantity_expression, context)

            if qty and qty > 0:
                item = ConfigurationItem.objects.create(
                    configuration=self,
                    material=rule.material,
                    quantity=qty,
                    unit_price=rule.material.cost_per_unit,
                    is_template_item=True,
                )
                processed.add(item.id)

        # Cleanup old template items not created this cycle
        self.items.filter(is_template_item=True).exclude(id__in=processed).delete()

    def evaluate_computed_variables(self, context):
        """
        Evaluates ProductComputedVariables, supporting:
        - constants (e.g. "50")
        - expressions (e.g. "width / 2")
        - dependencies between variables
        """

        computed_vars = list(self.product.computed_vars.all())
        unresolved = computed_vars.copy()
        last_count = -1

        # Multi-pass resolution: try until no progress is made
        while unresolved and len(unresolved) != last_count:
            last_count = len(unresolved)
            next_round = []

            for var in unresolved:
                try:
                    value = safe_eval(var.expression, context)
                    context[var.name] = value
                except Exception:
                    next_round.append(var)

            unresolved = next_round

        # If still unresolved → circular dependency or missing variables
        if unresolved:
            names = ", ".join(v.name for v in unresolved)
            raise ValueError(
                f"Could not resolve computed variables: {names}. "
                f"Check for circular dependencies or missing input variables."
            )

        return context



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
        Material,
        on_delete=models.PROTECT,
        help_text="Selectable component or part."
    )

    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1)

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

    is_template_item = models.BooleanField(
        default=True,
        help_text="Whether this item comes from a BOM rule or is a user add-on."
    )

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.material} x {self.quantity}"

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
    

class ConfigurationParameterValue(models.Model):
    configuration = models.ForeignKey(
        Configuration,
        on_delete=models.CASCADE,
        related_name="param_values"
    )

    parameter = models.ForeignKey(
        ProductParameter,
        on_delete=models.CASCADE,
        related_name="values"
    )

    value = models.CharField(max_length=200)

    def __str__(self):
        return f"{self.parameter.name} = {self.value}"
    
class ProductComputedVariable(models.Model):
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="computed_vars"
    )
    name = models.CharField(max_length=100)
    expression = models.TextField(
        help_text="Enter either a number or an expression. Example: 50, width / 2"
    )

    def __str__(self):
        return f"{self.product.name}.{self.name}"
