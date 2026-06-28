from django.db import models
from djmoney.models.fields import MoneyField
from decimal import Decimal
from django.utils.translation import gettext_lazy as _
from company.models import CompanyOwnedModel


class CommissionRuleType(models.TextChoices):
    PERCENTAGE = "percentage", _("Percentage (%)")
    FIXED = "fixed", _("Fixed amount")
    CUSTOM = "custom", _("Custom rule")

RULE_VALUE_FORMATTERS = {
    CommissionRuleType.PERCENTAGE: lambda value, custom_rule: f"{value}%" if value is not None else "-",
    CommissionRuleType.FIXED: lambda value, custom_rule: f"€{value:.2f}" if value is not None else "-",
    CommissionRuleType.CUSTOM: lambda value, custom_rule: str(custom_rule) if custom_rule else "-",
}


class CommissionRuleTemplate(models.Model):
    """
    A user-defined reusable commission rule expression.
    Shared across all companies.

    Available variables in the expression:
    - subtotal: configuration subtotal before tax
    - total: configuration total after tax
    - quantity: sum of quantities of all items in the configuration
    """

    name = models.CharField(
        max_length=100,
        verbose_name=_("Name"),
    )

    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        help_text=_("Optional explanation of what this rule does."),
    )

    expression = models.TextField(
        verbose_name=_("Expression"),
        help_text=_(
            "Expression evaluated with safe_eval.\n\n"
            "Available variables:\n"
            "  • subtotal — configuration subtotal before tax\n"
            "  • total    — configuration total after tax\n"
            "  • quantity — total quantity of items\n\n"
            "Examples:\n"
            "  • 5% of subtotal:                   subtotal * 0.05\n"
            "  • Flat €200:                        200\n"
            "  • 5% only if subtotal > €5,000:     subtotal * 0.05 if subtotal > 5000 else 0\n"
            "  • Tiered (3% under €5k, 5% above):  subtotal * 0.03 if subtotal < 5000 else subtotal * 0.05\n"
            "  • €50 per unit:                     quantity * 50\n"
            "  • 4% of after-tax total:            total * 0.04\n"
            "  • 3% + flat €25:                    (subtotal * 0.03) + 25\n"
        ),
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
    )

    class Meta:
        verbose_name = _("Commission rule template")
        verbose_name_plural = _("Commission rule templates")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def compute(self, subtotal: Decimal, total: Decimal, quantity: Decimal) -> Decimal:
        from configurator.utils import safe_eval
        context = {
            "subtotal": float(subtotal),
            "total": float(total),
            "quantity": float(quantity),
        }
        result = safe_eval(self.expression, context)
        return Decimal(str(result)).quantize(Decimal("0.01"))


class AgentCommissionRule(CompanyOwnedModel):
    """
    Defines what commission an employee earns when they sell
    a configuration based on a specific product template.
    One rule per (employee, product_template) pair.
    """

    product_template = models.ForeignKey(
        "configurator.ProductTemplate",
        on_delete=models.CASCADE,
        related_name="commission_rules",
        verbose_name=_("Product template"),
    )

    agent = models.ForeignKey(
        "sales.CommissionistAgent",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="commission_rules",
        verbose_name=_("Agent"),
    )

    rule_type = models.CharField(
        max_length=20,
        choices=CommissionRuleType.choices,
        default=CommissionRuleType.PERCENTAGE,
        verbose_name=_("Rule type"),
    )

    value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Value"),
        help_text=_(
            "For percentage: enter the rate (e.g. 5 means 5%). "
            "For fixed: enter the flat amount in the company currency. "
            "Leave blank when using a custom rule."
        ),
    )

    custom_rule = models.ForeignKey(
        CommissionRuleTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_rules",
        verbose_name=_("Custom rule"),
        help_text=_("Only used when rule type is 'Custom rule'."),
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
    )

    class Meta:
        unique_together = ("agent", "product_template")
        verbose_name = _("Agent commission rule")
        verbose_name_plural = _("Agent commission rules")
        ordering = ["agent", "product_template"]

    def format_value(self) -> str:
        """
        Returns a human-readable string of the rule value.
        Reusable by both the model __str__ and admin display methods.
        """
        formatter = RULE_VALUE_FORMATTERS.get(self.rule_type)
        if formatter:
            return formatter(self.value, self.custom_rule)
        return "-"

    def __str__(self):
        return f"{self.agent} — {self.product_template} [{self.format_value()}]"

    def compute_commission(
        self,
        subtotal: Decimal,
        total: Decimal = None,
        quantity: Decimal = None,
    ) -> Decimal:
        if total is None:
            total = subtotal
        if quantity is None:
            quantity = Decimal("0")

        if self.rule_type == CommissionRuleType.PERCENTAGE and self.value is not None:
            return (subtotal * self.value / Decimal("100")).quantize(Decimal("0.01"))
        elif self.rule_type == CommissionRuleType.FIXED and self.value is not None:
            return self.value.quantize(Decimal("0.01"))
        elif self.rule_type == CommissionRuleType.CUSTOM and self.custom_rule:
            return self.custom_rule.compute(subtotal, total, quantity)
        return Decimal("0.00")


def format_rule_snapshot(rule_type: str, rule_value) -> str:
    """
    Formats a rule_type + value snapshot for display in admin list views.
    Used by QuoteCommissionAdmin and any other place that shows snapshots.
    """
    if rule_value is None:
        return "-"
    if rule_type == CommissionRuleType.PERCENTAGE:
        return f"{rule_value}%"
    if rule_type == CommissionRuleType.FIXED:
        return f"€{rule_value:.2f}"
    return str(rule_value)


class QuoteCommission(models.Model):
    """
    One row per (configuration, employee) pair.
    """

    quote = models.ForeignKey(
        "sales.Quote",
        on_delete=models.CASCADE,
        related_name="commissions",
        verbose_name=_("Quote"),
        null=True,
        blank=True,
    )

    configuration = models.ForeignKey(
        "configurator.Configuration",
        on_delete=models.CASCADE,
        related_name="commissions",
        verbose_name=_("Configuration"),
        null=True,
        blank=True,
    )

    agent = models.ForeignKey(
        "sales.CommissionistAgent",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="quote_commissions",
        verbose_name=_("Agent"),
    )

    rule = models.ForeignKey(
        AgentCommissionRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quote_commissions",
        verbose_name=_("Commission rule used"),
    )

    commission_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Commission amount"),
    )

    rule_type_snapshot = models.CharField(
        max_length=20,
        choices=CommissionRuleType.choices,
        blank=True,
        verbose_name=_("Rule type"),
    )

    rule_value_snapshot = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Rule value"),
    )

    calculated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Last calculated at"),
    )

    class Meta:
        unique_together = ("configuration", "agent")
        verbose_name = _("Quote commission")
        verbose_name_plural = _("Quote commissions")
        ordering = ["quote", "configuration"]

    def __str__(self):
        agent = self.agent or _("No agent")
        return f"Commission for {agent} on {self.configuration}"

    @property
    def product_template(self):
        return self.configuration.product