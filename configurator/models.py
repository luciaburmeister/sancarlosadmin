from django.db import models
from djmoney.models.fields import MoneyField
from sales.models import Client
from timestamps.models import Timestampable
from materials.models import Component
from configurator.utils import safe_eval,_coerce_numeric,validate_expression_for_safe_eval
from django.core.exceptions import ValidationError
from .validators import pricing_expression_validator
from configurator.services import ConfigurationExportService
from decimal import Decimal
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import ArrayField
from viewflow.fsm import State

class ConfigurationStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    COMPLETED = "completed", _("Completed")
    CONVERTED = "converted", _("Converted to Quote")

class IncompatibilityRule(models.Model):
    """
    Material A is incompatible with Material B.
    """

    component_a = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        related_name="incompatible_with",
        null=True,
        verbose_name=_("Component A"),
    )
    component_b = models.ForeignKey(
        Component,
        on_delete=models.CASCADE,
        related_name="incompatible_with_reverse",
        null=True,
        verbose_name=_("Component B"),
    )

    reason = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Reason"),
    )

    class Meta:
        unique_together = ("component_a", "component_b")
        verbose_name = _("Incompatibility rule")
        verbose_name_plural = _("Incompatibility rules")

    def __str__(self):
        return f"{self.component_a} ❌ {self.component_b}"


    
class ProductTemplate(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    production_stages = ArrayField(
        base_field=models.CharField(
            max_length=50
        ),
        default=list,
        blank=True,
        help_text=_("Ordered list of possible production stages for this product"),
        verbose_name=_("Production Stages"),
    )
    image = models.ImageField(
        upload_to="products/",
        blank=True,
        null=True,
        verbose_name=_("Image"),
    )

    class Meta:
        verbose_name = _("Product template")
        verbose_name_plural = _("Product templates")

    def __str__(self):
        return self.name
    
    def clean(self):
        super().clean()

        if not self.production_stages:
            return

        stages = [s.strip() for s in self.production_stages]

        if any(not s for s in stages):
            raise ValidationError(
                {"production_stages": _("Stage keys cannot be empty.")}
            )

        if len(stages) != len(set(stages)):
            raise ValidationError(
                {"production_stages": _("Stage keys must be unique.")}
            )

        self.production_stages = stages

class ParameterType(models.TextChoices):
    NUMBER = "number", _("Number")
    TEXT = "text", _("Text")
    BOOLEAN = "boolean", _("Boolean")
    SELECT = "select", _("Select")

class ProductParameter(models.Model):
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="parameters",
        verbose_name=_("Product"),
    )

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    label = models.CharField(max_length=100, verbose_name=_("Label"))
    type = models.CharField(
        max_length=10,
        choices=ParameterType.choices,
        default=ParameterType.NUMBER,
        verbose_name=_("Type"),
    )
    options = models.JSONField(
        blank=True,
        null=True,
        default=list,
        verbose_name=_("Options"),
    )
    required = models.BooleanField(default=True, verbose_name=_("Required"))
    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Sorting Order"),
        help_text=_("Order of the parameter within the product"),
        db_index=True
    )

    class Meta:
        verbose_name = _("Product parameter")
        verbose_name_plural = _("Product parameters")
        ordering = ["order","product"]
        indexes = [
        models.Index(
            fields=["product", "order"],
        )
    ]

    def __str__(self):
        return f"{self.product_id} – {self.label}"

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
        related_name="bom_rules",
        verbose_name=_("Product"),
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.PROTECT,
        null=True,
        verbose_name=_("Component"),
    )

    quantity_expression = models.TextField(
        help_text=_("Expression evaluated with safe_eval"),
        verbose_name=_("Quantity expression"),
    )

    length_expression = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Length expression"),
    )

    description = models.CharField(
        max_length=40,
        null=True,
        blank=True,
        verbose_name=_("Description")
    )

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Production stages this component participates in"),
        verbose_name=_("Production Stages"),
    )

    class Meta:
        verbose_name = _("BOM rule")
        verbose_name_plural = _("BOM rules")


    EXPRESSION_FIELDS = {
        "quantity_expression": True,   # required
        "length_expression": True,    # optional
    }


    def __str__(self):
        return f"{self.product_id} → {self.component_id}"
    
    def clean(self):
        super().clean()

        errors = {}

        for field_name, required in self.EXPRESSION_FIELDS.items():
            raw = getattr(self, field_name)
            expr = (raw or "").strip()

            if expr:
                try:
                    validate_expression_for_safe_eval(expr)
                except ValueError as e:
                    errors[field_name] = f"Invalid expression: {e}"
        if self.production_stages:
            product_stages = set(self.product.production_stages or [])
            rule_stages = set(self.production_stages)

            if not rule_stages.issubset(product_stages):
                errors["production_stages"] = _(
                    "BOM rule stages must be a subset of the product production stages."
                )

        if errors:
            raise ValidationError(errors)


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
        verbose_name=_("Product"),
    )

    name = models.CharField(max_length=100, verbose_name=_("Name"))

    expression = models.TextField(
        validators=[pricing_expression_validator],
        help_text=_(
            "Reusable expression.\n"
            "Allowed variables:\n"
            "- base_total\n"
            "- material_cost\n"
            "- quantity\n"
            "- item_count\n"
            "Example: base_total * 1.10"
        ),
        verbose_name=_("Expression"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Is active"))

    class Meta:
        verbose_name = _("Pricing rule template")
        verbose_name_plural = _("Pricing rule templates")


    def __str__(self):
        return f"{self.name} ({self.expression})"
    


class Configuration(Timestampable):
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Client"),
    )

    name = models.CharField(
        max_length=200,
        help_text=_("Internal label for this configuration"),
        verbose_name=_("Name"),
    )

    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.PROTECT,
        related_name="configurations",
        null=True,
        verbose_name=_("Product"),
    )

    status = models.CharField( max_length=20, choices=ConfigurationStatus.choices, default=ConfigurationStatus.DRAFT, verbose_name=_("Status"), )

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total price"),
    )

    class Meta:
        verbose_name = _("Configuration")
        verbose_name_plural = _("Configurations")


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
            "material_cost": sum(float(i.component.cost_per_unit.amount) * float(i.quantity) for i in items),
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
        processed = set()

        context = self.get_pricing_context()

        for rule in self.product.bom_rules.all():
            qty = safe_eval(rule.quantity_expression, context)

            if not qty or qty <= 0:
                continue

            length = Decimal("1")
            if rule.length_expression:
                evaluated_length = safe_eval(rule.length_expression, context)
                if evaluated_length:
                    length = Decimal(str(evaluated_length))

            item = ConfigurationItem.objects.create(
                configuration=self,
                component=rule.component,
                quantity=qty,
                length=length,
                unit_price=rule.component.cost_per_unit,
                is_template_item=True,
                production_stages=rule.production_stages or [],
            )
            processed.add(item.id)

        self.items.filter(is_template_item=True).exclude(id__in=processed).delete()

    def evaluate_computed_variables(self, context):
        """
        Evaluates ProductComputedVariables, supporting:
        - constants (e.g. "50")
        - expressions (e.g. "width / 2")
        - dependencies between variables

        If some variables cannot be resolved, raises a ValueError
        listing each variable, its expression, and the last error.
        """
        context = {name: _coerce_numeric(value) for name, value in context.items()}

        computed_vars = list(self.product.computed_vars.all())
        unresolved = computed_vars.copy()
        last_count = -1
        errors: dict[str, str] = {}

        # Multi-pass resolution: try until no progress is made
        while unresolved and len(unresolved) != last_count:
            last_count = len(unresolved)
            next_round = []

            for var in unresolved:
                try:
                    value = safe_eval(var.expression, context)
                    context[var.name] = value
                    errors.pop(var.name, None)
                except ValueError as e:
                    # Likely missing variables or invalid expression → try next round
                    errors[var.name] = str(e)
                    next_round.append(var)
                except Exception as e:
                    errors[var.name] = f"{type(e).__name__}: {e}"
                    next_round.append(var)

            unresolved = next_round

        # If still unresolved → circular dependency or missing variables
        if unresolved:
            lines = []
            for v in unresolved:
                err = errors.get(v.name, "Unknown reason")
                lines.append(
                    f"- {v.name}: {v.expression!r} -> {err}"
                )

            details = "\n".join(lines)
            raise ValueError(
                "Could not resolve computed variables.\n"
                "Check for circular dependencies or missing input variables.\n"
                f"Details:\n{details}"
            )

        return context

    
    def available_export_templates(self):
        return ConfigurationExportService.get_available_templates(self)

    def render_export(self, template):
        return ConfigurationExportService.render_html(self, template)
    

class ConfigurationFlow:
    state = State(ConfigurationStatus, default=ConfigurationStatus.DRAFT)

    def __init__(self, configuration: Configuration):
        self.configuration = configuration

    @state.setter()
    def _set_status(self, value):
        self.configuration.status = value

    @state.getter()
    def _get_status(self):
        return self.configuration.status
    
    @state.transition(
        source=ConfigurationStatus.DRAFT,
        target=ConfigurationStatus.COMPLETED,
    )
    def complete(self):
        if not self.configuration.items.exists():
            raise ValidationError(
                "Cannot complete an empty configuration."
            )
    @state.transition(
        source=ConfigurationStatus.DRAFT,
        target=ConfigurationStatus.CONVERTED,
    )
    def convert(self):
        from production.services.configuration_to_production import (
            ConfigurationToProductionService
        )

        ConfigurationToProductionService.create_ticket(
            self.configuration
        )

    @state.on_success()
    def _persist(self, descriptor, source, target):
        self.configuration.save(update_fields=["status"])



class ConfigurationItem(Timestampable):
    """
    Materials or parts selected during configuration.
    Does NOT affect inventory or orders directly.
    """

    configuration = models.ForeignKey(
        Configuration,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Configuration"),
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.PROTECT,
        null=True,
        verbose_name=_("Component"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=1,
        verbose_name=_("Quantity"),
    )

    length = models.DecimalField(
        max_digits=15,
        decimal_places=3,
        default=1,
        null=True,
        help_text=_("Length factor used for pricing (e.g. meters)."),
        verbose_name=_("Length"),
    )

    is_template_item = models.BooleanField(
        default=True,
        help_text=_("Whether this item comes from a BOM rule or is a user add-on."),
        verbose_name=_("Is template item"),
    )

    line_total = MoneyField(
        max_digits=18,
        decimal_places=3,
        default_currency="EUR",
        editable=False,
        verbose_name=_("Line total")
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=5,
        default_currency="EUR",
        verbose_name=_("Unit price")
    )

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Production stages inherited from BOM rule"),
        verbose_name=_("Production stages"),
    )


    def save(self, *args, **kwargs):
        quantity = self.quantity or Decimal("0")
        length = self.length or Decimal("1")

        self.line_total = self.unit_price * quantity * length
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.component_id} x {self.quantity}"

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
        return f"{self.product_id}.{self.name}"
    
    def clean(self):
        super().clean()

        expr = (self.expression or "").strip()
        if not expr:
            raise ValidationError(
                {"expression": "Expression cannot be empty."}
            )

        try:
            validate_expression_for_safe_eval(expr)
        except ValueError as e:
            raise ValidationError(
                {"expression": f"Invalid expression: {e}"}
            )

class ConfigurationExportTemplate(models.Model):
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="export_templates",
        null=True,  # If null → can be used by any product
        blank=True
    )

    name = models.CharField(max_length=200)
    html = models.TextField(
        help_text="Django template syntax is allowed. Example: {{ config.total_price }}"
    )

    css = models.TextField(
        blank=True,
        help_text="CSS styling. Will be injected inside <style>...</style>"
    )

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} (Product {self.product_id or 'Any'})"
