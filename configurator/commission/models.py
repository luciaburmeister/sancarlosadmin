from django.db import models
from django.db.models import Sum
from djmoney.models.fields import MoneyField
from djmoney.money import Money
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
    - subtotal: quote subtotal before discounts
    - discounted_subtotal: quote subtotal after discounts
    - total: quote total after tax
    - quantity: sum of quantities across all items
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
            "  • subtotal — quote subtotal before discounts\n"
            "  • discounted_subtotal — quote subtotal after discounts\n"
            "  • total    — quote total after tax\n"
            "  • quantity — total quantity of items\n\n"
            "Examples:\n"
            "  • 5% of subtotal:                   subtotal * 0.05\n"
            "  • 5% after discounts:               discounted_subtotal * 0.05\n"
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

    def compute(self, subtotal: Decimal, discounted_subtotal: Decimal, total: Decimal, quantity: Decimal) -> Decimal:
        from configurator.utils import safe_eval
        context = {
            "subtotal": float(subtotal),
            "discounted_subtotal": float(discounted_subtotal),
            "total": float(total),
            "quantity": float(quantity),
        }
        result = safe_eval(self.expression, context)
        return Decimal(str(result)).quantize(Decimal("0.01"))


class AgentCommissionRule(CompanyOwnedModel):
    """
    Defines what commission an agent earns on a quote.
    One rule per agent.
    """

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
        unique_together = (("agent",),)
        verbose_name = _("Agent commission rule")
        verbose_name_plural = _("Agent commission rules")
        ordering = ["agent"]

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
        return f"{self.agent} [{self.format_value()}]"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active and self.agent_id:
            from configurator.commission.services import CommissionService

            CommissionService.recalculate_unruled_for_agent(self.agent_id)

    def compute_commission(
        self,
        subtotal: Decimal,
        discounted_subtotal: Decimal = None,
        total: Decimal = None,
        quantity: Decimal = None,
    ) -> Decimal:
        if discounted_subtotal is None:
            discounted_subtotal = subtotal
        if total is None:
            total = subtotal
        if quantity is None:
            quantity = Decimal("0")

        if self.rule_type == CommissionRuleType.PERCENTAGE and self.value is not None:
            return (subtotal * self.value / Decimal("100")).quantize(Decimal("0.01"))
        elif self.rule_type == CommissionRuleType.FIXED and self.value is not None:
            return self.value.quantize(Decimal("0.01"))
        elif self.rule_type == CommissionRuleType.CUSTOM and self.custom_rule:
            return self.custom_rule.compute(subtotal, discounted_subtotal, total, quantity)
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


class QuoteCommissionQuerySet(models.QuerySet):
    def with_released(self):
        """Annotate the released total so listing rows need no query each."""
        return self.annotate(released_total=Sum("releases__released_amount"))


class QuoteCommission(models.Model):
    """
    One row per (quote, agent) pair.
    Commission is calculated at the quote/basket level.
    """

    quote = models.ForeignKey(
        "sales.Quote",
        on_delete=models.CASCADE,
        related_name="commissions",
        verbose_name=_("Quote"),
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

    objects = QuoteCommissionQuerySet.as_manager()

    class Meta:
        unique_together = (("quote", "agent"),)
        verbose_name = _("Quote commission")
        verbose_name_plural = _("Quote commissions")
        ordering = ["quote"]

    def __str__(self):
        agent = self.agent or _("No agent")
        return f"Commission for {agent} on {self.quote}"

    @property
    def released_amount(self):
        if hasattr(self, "released_total"):
            total = self.released_total
        else:
            total = self.releases.aggregate(total=Sum("released_amount"))["total"]
        return Money(total or 0, self.commission_amount_currency)

    @property
    def pending_amount(self):
        return self.commission_amount - self.released_amount

    @property
    def release_status(self):
        released = self.released_amount.amount
        if released <= 0:
            return CommissionReleaseStatus.NONE
        if released >= self.commission_amount.amount:
            return CommissionReleaseStatus.FULL
        return CommissionReleaseStatus.PARTIAL


class InvoiceCommission(models.Model):
    """
    What one agent earns on one sales invoice and how much of it has been
    released: one row per (invoice, agent), the line of the commissions
    list. It adds up the invoice's quote commissions for that agent and the
    agent's own commission on the part no quote accounts for (imported
    invoices, direct sales), whose rule is frozen once the invoice is issued.
    Kept in step by CommissionReleaseService.sync_for_invoice.
    """

    sales_invoice = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.CASCADE,
        related_name="commissions",
        verbose_name=_("Sales invoice"),
    )
    agent = models.ForeignKey(
        "sales.CommissionistAgent",
        on_delete=models.CASCADE,
        related_name="invoice_commissions",
        verbose_name=_("Agent"),
    )
    base_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Commission base"),
    )
    commission_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Commission amount"),
    )
    released_base_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Base"),
        help_text=_("The part of the commission base already collected."),
    )
    released_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Released commission"),
    )
    release_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Release date"),
        help_text=_("Date of the last payment that released commission."),
    )
    # The part no quote accounts for, with the rule it was calculated with
    rule = models.ForeignKey(
        AgentCommissionRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_commissions",
        verbose_name=_("Commission rule used"),
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
    own_base_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Base without quote"),
    )
    own_commission_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Commission without quote"),
    )
    calculated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Last calculated at"),
    )

    class Meta:
        unique_together = (("sales_invoice", "agent"),)
        verbose_name = _("Commission")
        verbose_name_plural = _("Commissions")
        ordering = ["-release_date", "-id"]

    def __str__(self):
        return f"Commission for {self.agent} on {self.sales_invoice}"

    @property
    def commission_percentage(self):
        """The effective rate (3, 5…), on what is released or else on the whole."""
        released_base = self.released_base_amount.amount
        if released_base:
            return (self.released_amount.amount / released_base * Decimal("100")).quantize(Decimal("0.01"))
        if self.base_amount.amount:
            return (self.commission_amount.amount / self.base_amount.amount * Decimal("100")).quantize(Decimal("0.01"))
        return Decimal("0.00")

    @property
    def release_status(self):
        released = self.released_amount.amount
        commission = self.commission_amount.amount
        if not released:
            return CommissionReleaseStatus.NONE
        if abs(released) >= abs(commission):
            return CommissionReleaseStatus.FULL
        return CommissionReleaseStatus.PARTIAL


class CommissionReleaseStatus(models.TextChoices):
    NONE = "none", _("Not released")
    PARTIAL = "partial", _("Partially released")
    FULL = "full", _("Released")


class CommissionRelease(models.Model):
    """
    The commission an invoice has released for one agent: one row per
    payment allocated to it, or a single row when it is settled at once
    (agent paid on issue, credit note, invoice marked paid by hand). It
    belongs to the quote's commission for the part billed from a quote, or
    to the invoice's own commission for the rest.
    Rebuilt by CommissionReleaseService.sync_for_invoice on every change, so
    rows are never edited by hand.
    """

    agent = models.ForeignKey(
        "sales.CommissionistAgent",
        on_delete=models.CASCADE,
        related_name="commission_releases",
        verbose_name=_("Agent"),
    )
    invoice_commission = models.ForeignKey(
        InvoiceCommission,
        on_delete=models.CASCADE,
        related_name="releases",
        verbose_name=_("Commission"),
    )
    quote_commission = models.ForeignKey(
        QuoteCommission,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="releases",
        verbose_name=_("Quote commission"),
        help_text=_("Set for the part of the invoice billed from a quote; empty for the rest."),
    )
    sales_invoice = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.CASCADE,
        related_name="commission_releases",
        verbose_name=_("Sales invoice"),
    )
    allocation = models.ForeignKey(
        "payments.PaymentAllocation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="commission_releases",
        verbose_name=_("Payment allocation"),
        help_text=_("Empty when the agent's commission is released on issue."),
    )
    release_date = models.DateField(
        verbose_name=_("Release date"),
        help_text=_("Payment date, or the invoice's issue date when released on issue."),
    )
    base_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Base covered"),
        help_text=_("Net (taxable) base of the quote this release covers."),
    )
    released_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Released commission"),
    )

    class Meta:
        verbose_name = _("Commission release")
        verbose_name_plural = _("Commission releases")
        ordering = ["-release_date", "-id"]

    def __str__(self):
        return f"{self.released_amount} — {self.agent} on {self.sales_invoice} ({self.release_date})"