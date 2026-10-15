from collections.abc import Iterable

from django.db import models
import unicodedata
from djmoney.models.fields import MoneyField
from sales.models import Client, Quote
from timestamps.models import Timestampable
from materials.models import Component
from configurator.utils import (
    safe_eval,
    _coerce_numeric,
    validate_expression_for_safe_eval,
)
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from .validators import pricing_expression_validator
from configurator.services import ConfigurationExportService
from decimal import ROUND_HALF_UP, Decimal
from django.utils.translation import gettext_lazy as _
from django.contrib.postgres.fields import ArrayField
from viewflow.fsm import State
from company.models import CompanyOwnedModel, DocumentLanguage
from invoices.models import AppliedTax
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone
from configurator.commission.models import AgentCommissionRule, CommissionRelease, InvoiceCommission, QuoteCommission

class ConfigurationStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    COMPLETED = "completed", _("Completed")
    CONVERTED = "converted", _("Converted to Production")
    SHOP = "shop", _("Direct to Shop")
    PRODUCTION_DONE = "production_done", _("Production Done")
    CANCELLED = "cancelled", _("Cancelled")


class ConfigurationPlanningStatus(models.TextChoices):
    UNPLANNED = "unplanned", _("Unplanned")
    PLANNED = "planned", _("Planned")


class IncompatibilityRule(CompanyOwnedModel):
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


class ProductFlowKind(models.TextChoices):
    QUOTABLE = "quotable", _("Quotable")
    SHOP = "shop", _("Shop")


class ProductTemplate(CompanyOwnedModel):
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Ordered list of possible production stages for this product"),
        verbose_name=_("Production Stages"),
    )
    parameter_group_options = ArrayField(
        base_field=models.CharField(max_length=150),
        default=list,
        blank=True,
        help_text=_("Parameter Group Options to group in forms"),
        verbose_name=_("Parameter Group Options"),
    )
    image = models.ImageField(
        upload_to="products/",
        blank=True,
        null=True,
        verbose_name=_("Image"),
    )
    production_planning_resources = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Production planning resources"),
    )
    production_planning_configured = models.BooleanField(
        default=False,
        editable=False,
        verbose_name=_("Planning configured"),
    )
    show_in_configurator = models.BooleanField(
        default=True,
        verbose_name=_("Show in configurator"),
        help_text=_("If false, this template will not be shown in the manual configuration creation list.")
    )
    allow_production_without_accepted_quote = models.BooleanField(
        default=False,
        verbose_name=_("Allow production without accepted quote"),
        help_text=_("If enabled, configurations using this template can be converted to production even if the linked quote is not accepted yet."),
    )
    allow_parameter_values_edit_in_any_status = models.BooleanField(
        default=False,
        verbose_name=_("Allow changing parameter values in any status"),
        help_text=_("If enabled, parameter values can be edited even when the configuration is in production, completed, or any non-draft status."),
    )
    flow_kind = models.CharField(
        max_length=20,
        choices=ProductFlowKind.choices,
        default=ProductFlowKind.QUOTABLE,
        verbose_name=_("Flow Kind"),
    )
    exclude_from_invoicing = models.BooleanField(
        default=False,
        verbose_name=_("Exclude from invoicing"),
        help_text=_(
            "Configurations of this product are never included in the project file "
            "invoiceable summary or invoices (e.g. vehicle reception). They also skip "
            "production ticket and receival slip creation in the shop flow."
        ),
    )
    exclude_from_commission = models.BooleanField(
        default=False,
        verbose_name=_("No commission"),
        help_text=_(
            "Configurations of this product earn agents no commission "
            "(e.g. portes, transport): they are left out of the commission base "
            "of quotes and invoices."
        ),
    )
    unit_price_variable = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name=_("Unit price variable"),
        help_text=_(
            "Name of a computed variable to use as divisor for unit price. "
            "unit_price = subtotal / variable_value. Leave blank to disable."
        ),
    )
    is_labor_hours_template = models.BooleanField(
        default=False,
        verbose_name=_("Labor hours template"),
        help_text=_(
            "If enabled, Configurations built from this template are treated as billed "
            "labor hours: their 'units' value is summed as billed hours per expediente "
            "and compared against real worked hours (from production sessions) in the "
            "labor hours margin dashboard."
        ),
    )

    class Meta:
        verbose_name = _("Product template")
        verbose_name_plural = _("Product templates")
        permissions = [
            ("configure_stage_resources", _("Configure stage resources")),
            ("labor_hours_margin_dashboard", _("Can view Labor Hours Margin Dashboard")),
        ]
        indexes = [
            models.Index(fields=["show_in_configurator","company"]),
        ]

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

        if self.company:
            company_stages = set(self.company.production_stages or [])
            if not set(stages).issubset(company_stages):
                raise ValidationError(
                    {"production_stages": _("Product stages must be a subset of the company production stages.")}
                )

        if not self.production_planning_resources:
            return

        stage_resources_keys = set(self.production_planning_resources)

        if stage_resources_keys - set(stages):
            raise ValidationError(
                {
                    "production_planning_resources": _(
                        "Stage resources keys must be the same as stage keys"
                    )
                }
            )

    def clone(self, *, name_suffix=" (copy)", owner=None):
        """
        Deep copy this ProductTemplate and all related template data.
        Returns the newly created ProductTemplate.
        """
        with transaction.atomic():
            product_copy = ProductTemplate.objects.create(
                company=owner or self.company,
                name=f"{self.name}{name_suffix}",
                description=self.description,
                production_stages=list(self.production_stages or []),
                image=self.image,
                is_labor_hours_template=self.is_labor_hours_template,
            )

            self._clone_parameters(product_copy)

            self._clone_bom_rules(product_copy)

            self._clone_pricing_rules(product_copy)

            self._clone_computed_variables(product_copy)

            self._clone_export_templates(product_copy)

            self._clone_computed_templates(product_copy)

            return product_copy

    def _clone_parameters(self, product_copy):
        params = []
        for p in self.parameters.all():
            params.append(
                ProductParameter(
                    product=product_copy,
                    name=p.name,
                    label=p.label,
                    type=p.type,
                    options=p.options,
                    required=p.required,
                    order=p.order,
                    default_value=p.default_value,
                )
            )
        ProductParameter.objects.bulk_create(params)

    def _clone_bom_rules(self, product_copy):
        rules = []
        for r in self.bom_rules.all():
            rules.append(
                BOMRule(
                    company=product_copy.company,
                    product=product_copy,
                    component=r.component,
                    quantity_expression=r.quantity_expression,
                    length_expression=r.length_expression,
                    description=r.description,
                    production_stages=list(r.production_stages or []),
                )
            )
        BOMRule.objects.bulk_create(rules)

    def _clone_pricing_rules(self, product_copy):
        defaults = []
        for a in self.pricing_rule_defaults.select_related("rule").all():
            defaults.append(
                ProductPricingRuleAssignment(
                    product=product_copy,
                    rule=a.rule,
                    enabled=a.enabled,
                    order=a.order,
                )
            )
        ProductPricingRuleAssignment.objects.bulk_create(defaults)

    def _clone_computed_variables(self, product_copy):
        vars = []
        for v in self.computed_vars.all():
            vars.append(
                ProductComputedVariable(
                    company=product_copy.company,
                    product=product_copy,
                    name=v.name,
                    expression=v.expression,
                )
            )
        ProductComputedVariable.objects.bulk_create(vars)

    def _clone_export_templates(self, product_copy):
        templates = []
        for t in self.export_templates.all():
            templates.append(
                ConfigurationExportTemplate(
                    company=product_copy.company,
                    product=product_copy,
                    name=t.name,
                    html=t.html,
                    css=t.css,
                    is_active=t.is_active,
                )
            )
        ConfigurationExportTemplate.objects.bulk_create(templates)


    def _clone_computed_templates(self, product_copy):
        templates = []
        for t in self.computed_templates.all():
            templates.append(
                ProductComputedTemplate(
                    company=product_copy.company,
                    product=product_copy,
                    name=t.name,
                    condition_expression=t.condition_expression,
                    template=t.template,
                    order=t.order,
                )
            )
        ProductComputedTemplate.objects.bulk_create(templates)


class ProductTemplateComponentGroup(models.Model):
    product_template = models.ForeignKey(
        "configurator.ProductTemplate",
        on_delete=models.CASCADE,
        related_name="component_groups",
        verbose_name=_("Product template"),
    )

    stage = models.CharField(
        max_length=50,
        verbose_name=_("Stage"),
        help_text=_("Stage where these components move together"),
    )

    components = models.ManyToManyField(
        Component,
        blank=True,
        related_name="component_groups",
        verbose_name=_("Components"),
    )

    class Meta:
        verbose_name = _("Stage component group")
        verbose_name_plural = _("Stage component groups")

    def __str__(self) -> str:
        return f"{self.product_template} — {self.stage}"

    def clean(self):
        super().clean()
        if not self.stage:
            return

        self.stage = unicodedata.normalize("NFC", self.stage.strip())

        if self.product_template:
            valid_stages = self.product_template.production_stages or []
            if self.stage not in valid_stages:
                raise ValidationError(
                    {
                        "stage": _(
                            "Stage '%(stage)s' is not defined in the product template stages."
                        )
                        % {"stage": self.stage}
                    }
                )


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
        db_index=True,
    )
    parameter_group = models.CharField(
        null=True, verbose_name=_("Parameter Group"), blank=True
    )
    default_value = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        verbose_name=_("Default Value"),
        help_text=_("Default value for this parameter"),
    )

    class Meta:
        verbose_name = _("Product parameter")
        verbose_name_plural = _("Product parameters")
        ordering = ["order", "product"]
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


class BOMRule(CompanyOwnedModel):
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
        max_length=100, null=True, blank=True, verbose_name=_("Description")
    )

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Production stages this component participates in"),
        verbose_name=_("Production Stages"),
    )

    note_expression = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Note expression"),
        help_text=_("Expression to calculate a note stored in the configuration item."),
    )

    class Meta:
        verbose_name = _("BOM rule")
        verbose_name_plural = _("BOM rules")

    EXPRESSION_FIELDS = {
        "quantity_expression": True,  # required
        "length_expression": False,   # optional
        "note_expression": False,     # optional
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
            self.production_stages = [
                unicodedata.normalize("NFC", s.strip()) for s in self.production_stages
            ]
            product_stages = set(self.product.production_stages or [])
            rule_stages = set(self.production_stages)

            if not rule_stages.issubset(product_stages):
                errors["production_stages"] = _(
                    "BOM rule stages must be a subset of the product production stages."
                )

        if errors:
            raise ValidationError(errors)


class PricingRule(CompanyOwnedModel):
    """
    A reusable pricing rule template that can be applied
    to many configurations.
    """

    name = models.CharField(max_length=100, verbose_name=_("Name"))

    expression = models.TextField(
        help_text=_(
            "Reusable expression.\n"
            "Always available: base_total, material_cost, quantity, item_count.\n"
            "Product parameters and computed variables of the product template "
            "can also be used; see the 'Available variables' section below.\n"
            "Example: base_total * 1.10"
        ),
        verbose_name=_("Expression"),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Is active"))

    label = models.CharField(
        max_length=100,
        blank=True,
        help_text=_("Human-readable label for exports (e.g. Margin, Discount)"),
        verbose_name=_("Label"),
        null=True,
    )

    show_in_export = models.BooleanField(
        default=False, verbose_name=_("Show in export")
    )

    class Meta:
        verbose_name = _("Pricing rule template")
        verbose_name_plural = _("Pricing rule templates")

    def __str__(self):
        return f"{self.name} ({self.expression})"


class ConfigurationQuerySet(models.QuerySet):
    def delete(self) -> tuple[int, dict[str, int]]:
        quote_ids = set(self.values_list("quote_id", flat=True))
        deleted = super().delete()
        self.model.recalc_quote_totals(quote_ids)
        return deleted


class Configuration(Timestampable, CompanyOwnedModel):
    objects = ConfigurationQuerySet.as_manager()

    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Client"),
    )

    quote = models.ForeignKey(
        'sales.Quote',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='configurations',
        verbose_name=_("Quote"),
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

    status = models.CharField(
        max_length=20,
        choices=ConfigurationStatus.choices,
        default=ConfigurationStatus.DRAFT,
        verbose_name=_("Status"),
    )

    quantity = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1)],
        verbose_name=_("Quantity"),
        help_text=_(
            "Identical pieces of this configuration. Prices and units are "
            "multiplied by it; the BOM stays per piece and each piece gets "
            "its own production ticket."
        ),
    )

    planning_status = models.CharField(
        max_length=20,
        choices=ConfigurationPlanningStatus.choices,
        default=ConfigurationPlanningStatus.UNPLANNED,
        db_index=True,
        verbose_name=_("Planning status"),
        help_text=_("Kept in sync automatically from the production ticket's planned sessions."),
    )

    planned_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Planned at"),
    )

    total_cost = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total cost"),
    )

    subtotal_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Subtotal (before tax)"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Unit price"),
        help_text=_("Subtotal divided by the product's unit price variable."),
    )

    units = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=0,
        verbose_name=_("Units"),
        help_text=_("Value of the unit price variable (divisor). Stored for invoice generation."),
    )

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total price (after tax)"),
    )

    price_override = MoneyField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        default_currency="EUR",
        verbose_name=_("Price override"),
        help_text=_("If set, this price will be used as subtotal, bypassing pricing rules.")
    )

    tax_breakdown = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Tax breakdown"),
        help_text=_(
            "Snapshot of tax calculations at pricing time. "
            "Used for exports and auditing."
        ),
    )

    pricing_breakdown = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Pricing breakdown"),
        help_text=_(
            "Snapshot of pricing rule calculations at pricing time. "
            "Used for exports and auditing."
        ),
    )

    computed_variables = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Computed variables"),
        help_text=_(
            "Snapshot of the product's computed variable values at pricing time."
        ),
    )

    rendered_templates = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Rendered templates"),
        help_text=_(
            "HTML/SVG templates rendered at pricing time. "
            "Used for exports."
        ),
    )

    asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="configurations",
        verbose_name=_("Asset"),
    )

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="direct_configurations",
        verbose_name=_("Project File"),
        help_text=_("Direct link to a project file for non-quotable configurations."),
    )

    work_order_pdf = models.FileField(
        upload_to="configurations/work_orders/",
        null=True,
        blank=True,
        verbose_name=_("Work Order PDF")
    )

    quote_group = models.ForeignKey(
        "sales.QuoteConfigurationGroup",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="configurations",
        verbose_name=_("Quote Group"),
        help_text=_("Optional group for organising configurations within a quote."),
    )

    notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Notes"),
        help_text=_("Free-form notes for this configuration."),
    )

    class Meta:
        verbose_name = _("Configuration")
        verbose_name_plural = _("Configurations")

    def __str__(self):
        return f"Config #{self.id} — {self.name or 'Unnamed'}"

    @classmethod
    def recalc_quote_totals(cls, quote_ids: Iterable[int | None]) -> None:
        ids = {quote_id for quote_id in quote_ids if quote_id}
        if not ids:
            return
        for quote in Quote.objects.filter(pk__in=ids):
            quote.recalc_totals()

    def save(self, *args, **kwargs) -> None:
        update_fields = kwargs.get("update_fields")
        quote_link_saved = update_fields is None or bool(
            {"quote", "quote_id"}.intersection(update_fields)
        )
        previous_quote_id = None
        if quote_link_saved and self.pk:
            previous_quote_id = (
                Configuration.objects.filter(pk=self.pk)
                .values_list("quote_id", flat=True)
                .first()
            )
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new or not quote_link_saved or previous_quote_id == self.quote_id:
            return
        self.recalc_quote_totals([previous_quote_id, self.quote_id])

    def delete(self, *args, **kwargs) -> tuple[int, dict[str, int]]:
        quote_id = self.quote_id
        deleted = super().delete(*args, **kwargs)
        self.recalc_quote_totals([quote_id])
        return deleted

    @property
    def invoice_quantity(self) -> Decimal:
        """Quantity a delivery order / invoice line carries for this
        configuration: its units when the product has a unit price variable,
        otherwise the number of pieces."""
        if self.units and self.units > 0:
            return self.units
        return Decimal(self.quantity or 1)

    def clone(self, *, name_suffix=" (copy)"):
        """
        Create an independent DRAFT copy with the same parameter values.
        Pricing, taxes and BOM are regenerated rather than copied verbatim,
        since product defaults may have changed since this was created.
        """
        with transaction.atomic():
            new_config = Configuration.objects.create(
                company=self.company,
                client=self.client,
                quote=self.quote,
                product=self.product,
                asset=self.asset,
                project_file=self.project_file,
                name=f"{self.name}{name_suffix}",
                notes=self.notes,
                quantity=self.quantity,
                status=ConfigurationStatus.DRAFT,
            )
            new_config.copy_default_pricing_rules_from_product()
            new_config.copy_default_taxes_from_product()
            new_config.copy_taxes_from_client()

            if self.price_override is not None:
                new_config.price_override = self.price_override
                new_config.save(update_fields=["price_override", "price_override_currency"])

            if self.product_id:
                params_by_name = {p.name: p for p in self.product.parameters.all()}
                ConfigurationParameterValue.objects.bulk_create([
                    ConfigurationParameterValue(
                        configuration=new_config,
                        parameter=param,
                        value=pv.value,
                        order=param.order,
                    )
                    for pv in self.param_values.select_related("parameter")
                    if (param := params_by_name.get(pv.parameter.name))
                ])
                new_config.generate_bom()
                new_config.recalculate_price()

            return new_config

    def get_pricing_context(self):
        """
        Builds the full evaluation context used for:
        - Pricing rules
        - Computed product variables
        - BOM expressions
        """

        # 1️⃣ Start with safe defaults for every declared template parameter so
        # BOM/pricing expressions never crash on missing keys when the user
        # leaves an optional field blank or before all values are submitted.
        context = {}
        if self.product_id:
            for p in self.product.parameters.all():
                if p.default_value not in (None, ""):
                    context[p.name] = p.normalize_value(p.default_value)
                elif p.type == ParameterType.NUMBER:
                    context[p.name] = 0
                elif p.type == ParameterType.BOOLEAN:
                    context[p.name] = False
                else:
                    context[p.name] = ""

        # 2️⃣ Overlay the actual user-supplied parameter values.
        prefetched_values = getattr(self, "_prefetched_objects_cache", {}).get(
            "param_values"
        )
        values = (
            prefetched_values
            if prefetched_values is not None
            else self.param_values.select_related("parameter")
        )
        for pv in values:
            context[pv.parameter.name] = pv.parameter.normalize_value(pv.value)

        # 3️⃣ Basic aggregates (always needed)
        items = self.items.select_related("component")

        context.update(
            {
                "material_cost": sum(
                    float(i.component.cost_per_unit.amount) * float(i.quantity)
                    for i in items
                ),
                "quantity": sum(float(i.quantity) for i in items),
                "item_count": items.count(),
                "base_total": sum(float(i.line_total.amount) for i in items),
            }
        )

        # 4️⃣ Evaluate product's computed variables (constants + expressions)
        context = self.evaluate_computed_variables(context)

        return context

    def calculate_taxes_and_snapshot(self, base_amount: Decimal):
        """
        Applies every tax (IVA, recargo, IRPF) to the taxable base, as
        invoices do — retentions subtract — and returns:
        - final total
        - tax breakdown snapshot (JSON-serializable)
        """
        breakdown = []
        base = Decimal(base_amount)
        total = base

        for tax in self.get_taxes():
            tax.base_amount = base
            tax.save()
            tax.calculate_and_store()

            result = tax.calculate(base)
            if tax.tax_type.behavior == "add":
                total = (total + result["tax_amount"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                total = (total - result["tax_amount"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            breakdown.append(
                {
                    "tax_name": tax.tax_type.tax_name,
                    "percentage": str(tax.selected_percentage),
                    "base": str(base.quantize(Decimal("0.01"))),
                    "tax_amount": str(result["tax_amount"]),
                    "total_after_tax": str(total),
                }
            )

        return total, breakdown

    def recalculate_price(self):
        if self.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE, ConfigurationStatus.SHOP]:
            return
        context = self.get_pricing_context()
        self.computed_variables = self._snapshot_computed_variables(context)

        base_subtotal = Decimal(str(context["base_total"]))
        pricing_lines = []
        # One entry per enabled rule, in assignment order, whether or not it
        # ended up changing the price. Shown in the admin next to each rule.
        rule_results: dict[int, dict] = {}
        pre_discount_subtotal = Decimal("0.00")
        discount_total = Decimal("0.00")

        if self.price_override is not None and self.price_override.amount >= 0:
            subtotal = Decimal(str(self.price_override.amount))
            pricing_lines.append(
                {
                    "label": str(_("Price Override")),
                    "type": "override",
                    "amount": str(subtotal.quantize(Decimal("0.01"))),
                }
            )
            pre_discount_subtotal = subtotal
        else:
            subtotal = base_subtotal
            assignments = list(
                self.pricing_rules.select_related("rule").filter(enabled=True)
            )

            for assignment in assignments:
                rule = assignment.rule
                amount = Decimal(str(safe_eval(rule.expression, context)))
                rule_results[rule.pk] = {
                    "rule_id": rule.pk,
                    "name": rule.name,
                    "label": rule.label or rule.name,
                    "expression": rule.expression,
                    "amount": str(amount.quantize(Decimal("0.01"))),
                    "applied": False,
                    "type": None,
                }

                if amount <= 0:
                    continue

                subtotal_before = subtotal
                subtotal += amount
                pricing_lines.append(
                    {
                        "rule_id": rule.pk,
                        "label": rule.label or rule.name,
                        "type": "charge",
                        "expression": rule.expression,
                        "subtotal_before": str(subtotal_before.quantize(Decimal("0.01"))),
                        "amount": str(amount.quantize(Decimal("0.01"))),
                        "subtotal_after": str(subtotal.quantize(Decimal("0.01"))),
                    }
                )
                rule_results[rule.pk].update(
                    {
                        "applied": True,
                        "type": "charge",
                        "subtotal_after": str(subtotal.quantize(Decimal("0.01"))),
                    }
                )

                context["base_total"] = float(subtotal)

            pre_discount_subtotal = subtotal

            for assignment in assignments:
                rule = assignment.rule
                amount = Decimal(str(safe_eval(rule.expression, context)))
                if not rule_results[rule.pk]["applied"]:
                    rule_results[rule.pk]["amount"] = str(
                        amount.quantize(Decimal("0.01"))
                    )

                if amount >= 0:
                    continue

                subtotal_before = subtotal
                subtotal += amount
                discount_total += amount

                if rule.show_in_export:
                    pricing_lines.append(
                        {
                            "rule_id": rule.pk,
                            "label": rule.label or rule.name,
                            "type": "discount",
                            "expression": rule.expression,
                            "subtotal_before": str(
                                subtotal_before.quantize(Decimal("0.01"))
                            ),
                            "amount": str(amount.quantize(Decimal("0.01"))),
                            "subtotal_after": str(subtotal.quantize(Decimal("0.01"))),
                        }
                    )
                rule_results[rule.pk].update(
                    {
                        "applied": True,
                        "type": "discount",
                        "amount": str(amount.quantize(Decimal("0.01"))),
                        "show_in_export": rule.show_in_export,
                        "subtotal_after": str(subtotal.quantize(Decimal("0.01"))),
                    }
                )

                context["base_total"] = float(subtotal)

        # Everything above priced one piece; the stored totals, units and
        # breakdown lines are for all of them (rule_results stay per piece).
        pieces = Decimal(self.quantity or 1)
        piece_subtotal = subtotal
        subtotal = piece_subtotal * pieces
        for line in pricing_lines:
            for key in ("amount", "subtotal_before", "subtotal_after"):
                if key in line:
                    line[key] = str((Decimal(line[key]) * pieces).quantize(Decimal("0.01")))

        self.total_cost = base_subtotal * pieces
        self.subtotal_price = subtotal

        # Compute unit price if the product defines a divisor variable
        unit_var_name = (self.product.unit_price_variable or "").strip()
        if unit_var_name and unit_var_name in context:
            divisor = Decimal(str(context[unit_var_name]))
            if divisor:
                self.unit_price = (piece_subtotal / divisor).quantize(Decimal("0.01"))
                self.units = divisor * pieces
            else:
                self.unit_price = Decimal("0")
                self.units = Decimal("0")
        else:
            self.unit_price = Decimal("0")
            self.units = Decimal("0")

        total, tax_breakdown = self.calculate_taxes_and_snapshot(subtotal)

        self.total_price = total
        self.tax_breakdown = tax_breakdown

        self.pricing_breakdown = {
            "quantity": int(pieces),
            "piece_subtotal": str(piece_subtotal.quantize(Decimal("0.01"))),
            "base_subtotal": str((base_subtotal * pieces).quantize(Decimal("0.01"))),
            "pre_discount_subtotal": str(
                (pre_discount_subtotal * pieces).quantize(Decimal("0.01"))
            ),
            "discount_total": str((discount_total * pieces).quantize(Decimal("0.01"))),
            "post_discount_subtotal": str(subtotal.quantize(Decimal("0.01"))),
            "lines": pricing_lines,
            "rules": list(rule_results.values()),
        }

        from configurator.services.export.service import ConfigurationExportService
        self.rendered_templates = ConfigurationExportService.evaluate_configuration_templates(self)

        self.save(
            update_fields=[
                "total_cost",
                "subtotal_price",
                "unit_price",
                "units",
                "total_price",
                "pricing_breakdown",
                "computed_variables",
                "tax_breakdown",
                "rendered_templates",
            ]
        )

        if getattr(self, 'quote_id', None):
            self.quote.recalc_totals()

    def _snapshot_computed_variables(self, context):
        """
        JSON-safe copy of the product's computed variable values from an
        already evaluated pricing context, keyed by variable name.
        """
        if not self.product_id:
            return {}

        def json_safe(value):
            if isinstance(value, bool) or value is None:
                return value
            if isinstance(value, Decimal):
                value = float(value)
            if isinstance(value, float) and value.is_integer():
                return int(value)
            if isinstance(value, (int, float, str)):
                return value
            return str(value)

        return {
            var.name: json_safe(context.get(var.name))
            for var in self.product.computed_vars.all()
        }

    def _evaluate_bom_rule(self, rule, context):
        """
        Evaluate a BOMRule into concrete quantity and length.
        Returns (quantity, length) or (None, None) if rule is inactive.
        """
        qty = safe_eval(rule.quantity_expression, context)
        if not qty or qty <= 0:
            return None, None

        length = Decimal("1")
        if rule.length_expression:
            evaluated_length = safe_eval(rule.length_expression, context)
            if evaluated_length:
                length = Decimal(str(evaluated_length))

        return Decimal(str(qty)), length

    def _bom_item_key(self, component_id):
        """
        Defines identity of a BOM-derived ConfigurationItem.
        Adjust here if you later support multiple BOM items per component.
        """
        return (component_id,)

    def _get_existing_bom_items(self):
        """
        Returns a dict mapping BOM identity → ConfigurationItem
        """
        items = self.items.filter(is_template_item=True)
        return {self._bom_item_key(item.component_id): item for item in items}
    
    def _get_item_stage_resources_snapshot(self, *, stages: list[str]) -> dict:
        """
        Returns the subset of product.production_planning_resources
        relevant to the given stages.
        """
        product_resources = self.product.production_planning_resources or {}

        return {stage: product_resources.get(stage, {}) for stage in (stages or [])}

    def generate_bom(self):
        if self.status not in [ConfigurationStatus.DRAFT, ConfigurationStatus.PRODUCTION_DONE, ConfigurationStatus.SHOP]:
            return
        """
        Incrementally reconciles BOM rules with existing ConfigurationItems.

        - Updates existing items linked to the same rule
        - Creates missing items for new rules
        - Deletes items that no longer have a corresponding rule
        """
        context = self.get_pricing_context()

        # Map existing items by their rule ID for easy lookup
        # We only consider items that are already linked to a rule
        existing_items = {
            item.bom_rule_id: item 
            for item in self.items.filter(is_template_item=True, bom_rule__isnull=False)
        }
        
        seen_item_ids = []

        rules = self.product.bom_rules.select_related("component").prefetch_related(
            "component__materials", "component__price_rules"
        )
        for rule in rules:
            qty, length = self._evaluate_bom_rule(rule, context)
            if qty is None:
                continue

            stages = rule.production_stages or []
            stage_resources = self._get_item_stage_resources_snapshot(stages=stages)

            if rule.id in existing_items:
                item = existing_items[rule.id]
                item.quantity = qty
                item.length = length
                item.unit_price = rule.component.compute_unit_price(context)
                item.production_stages = stages
                item.stage_resources = stage_resources
                item.note = safe_eval(rule.note_expression, context) if rule.note_expression else ""
                item.save()
            else:
                item = ConfigurationItem.objects.create(
                    configuration=self,
                    bom_rule=rule,
                    component=rule.component,
                    quantity=qty,
                    length=length,
                    unit_price=rule.component.compute_unit_price(context),
                    is_template_item=True,
                    production_stages=stages,
                    stage_resources=stage_resources,
                    note=safe_eval(rule.note_expression, context) if rule.note_expression else "",
                )
            
            seen_item_ids.append(item.id)

        self.items.filter(is_template_item=True).exclude(id__in=seen_item_ids).delete()

    def resync_planning_from_template(self):
        """
        Re-snapshots stage_resources on every ConfigurationItem from
        the product template's current production_planning_resources.

        Call this when the template's planning config has changed and
        existing configurations need to pick up the new resource
        definitions.
        """
        if not self.product_id:
            return 0

        product_resources = self.product.production_planning_resources or {}
        updated = 0

        for item in self.items.all():
            new_stage_resources = {
                stage: product_resources.get(stage, {})
                for stage in (item.production_stages or [])
            }
            if item.stage_resources != new_stage_resources:
                item.stage_resources = new_stage_resources
                item.save(update_fields=["stage_resources"])
                updated += 1

        return updated

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
                lines.append(f"- {v.name}: {v.expression!r} -> {err}")

            details = "\n".join(lines)
            raise ValueError(
                "Could not resolve computed variables.\n"
                "Check for circular dependencies or missing input variables.\n"
                f"Details:\n{details}"
            )

        return context

    def copy_default_taxes_from_product(self):
        """Copy taxes from the product template, falling back to company defaults.

        Tax priority:
        1. Product template taxes
        2. Company default taxes (fallback)
        """
        config_ct = ContentType.objects.get_for_model(self)

        # Try product taxes first
        copied = False
        if self.product_id:
            product_ct = ContentType.objects.get_for_model(self.product)
            defaults = AppliedTax.objects.filter(
                content_type=product_ct,
                object_id=self.product_id,
            )
            for tax in defaults:
                AppliedTax.objects.get_or_create(
                    content_type=config_ct,
                    object_id=self.pk,
                    tax_type=tax.tax_type,
                    defaults={
                        "selected_percentage": tax.selected_percentage,
                        "order": tax.order,
                    },
                )
                copied = True

        # Fallback: company default taxes
        if not copied and self.company_id:
            for i, cdt in enumerate(self.company.company_default_taxes.select_related("tax_type").all()):
                AppliedTax.objects.get_or_create(
                    content_type=config_ct,
                    object_id=self.pk,
                    tax_type=cdt.tax_type,
                    defaults={
                        "selected_percentage": cdt.selected_percentage,
                        "order": i,
                    },
                )

    def copy_taxes_from_client(self):
        """Copy the client's configured taxes to this configuration.

        When the company flag ``auto_apply_client_taxes`` is ON and the
        client has AppliedTax records, those taxes **replace** any existing
        taxes on this configuration (e.g. those copied from the product).
        """
        if not self.client_id or not self.company_id:
            return
        if not self.company.auto_apply_client_taxes:
            return

        client_ct = ContentType.objects.get_for_model(self.client)
        client_taxes = AppliedTax.objects.filter(
            content_type=client_ct,
            object_id=self.client_id,
        ).select_related("tax_type").order_by("order")

        if not client_taxes.exists():
            return

        # Remove existing taxes on this configuration (from product defaults)
        config_ct = ContentType.objects.get_for_model(self)
        AppliedTax.objects.filter(
            content_type=config_ct,
            object_id=self.pk,
        ).delete()

        # Copy client taxes
        for tax in client_taxes:
            AppliedTax.objects.create(
                content_type=config_ct,
                object_id=self.pk,
                tax_type=tax.tax_type,
                selected_percentage=tax.selected_percentage,
                order=tax.order,
            )

    def get_taxes(self):
        ct = ContentType.objects.get_for_model(self)
        return (
            AppliedTax.objects.filter(
                content_type=ct,
                object_id=self.pk,
            )
            .select_related("tax_type")
            .order_by("order")
        )

    def copy_default_pricing_rules_from_product(self):
        if not self.product_id:
            return

        defaults = self.product.pricing_rule_defaults.select_related("rule").all()
        for d in defaults:
            PricingRuleAssignment.objects.get_or_create(
                configuration=self,
                rule=d.rule,
                defaults={"enabled": d.enabled},
            )

    def available_export_templates(self):
        return ConfigurationExportService.get_available_templates(self)

    def render_export(self, template):
        return ConfigurationExportService.render_html(self, template)

    def recompute_component_groups(self):
        for group in self.component_groups.prefetch_related("items"):
            material_total = sum(item.line_total.amount for item in group.items.all())

            pricing_adjustment = self.evaluate_pricing_delta(material_total)

            group.material_total = material_total
            group.pricing_adjustment = pricing_adjustment
            group.total = material_total + pricing_adjustment

            group.save(
                update_fields=[
                    "material_total",
                    "pricing_adjustment",
                    "total",
                ]
            )

    def evaluate_pricing_delta(self, base_total: Decimal) -> Decimal:
        """
        Evaluates pricing rules using the given base_total
        and returns ONLY the pricing delta (margin / surcharge).
        """
        context = self.get_pricing_context()
        context["base_total"] = float(base_total)

        pricing_delta = Decimal("0.00")
        subtotal = Decimal(base_total)

        for assignment in self.pricing_rules.select_related("rule").filter(
            enabled=True
        ):
            rule = assignment.rule
            amount = Decimal(str(safe_eval(rule.expression, context)))

            if amount == 0:
                continue

            subtotal += amount
            pricing_delta += amount
            context["base_total"] = float(subtotal)

        return pricing_delta


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
            raise ValidationError("Cannot complete an empty configuration.")

    @state.transition(
        source=[ConfigurationStatus.DRAFT, ConfigurationStatus.COMPLETED],
        target=ConfigurationStatus.CONVERTED,
    )
    def convert(self):
        from production.services.configuration_to_production import (
            ConfigurationToProductionService,
        )

        project_file = None
        if self.configuration.quote_id and self.configuration.quote.project_file_id:
            project_file = self.configuration.quote.project_file
        elif self.configuration.project_file_id:
            project_file = self.configuration.project_file

        return ConfigurationToProductionService.create_ticket(
            self.configuration, project_file=project_file
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
        verbose_name=_("Line total"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=5,
        default_currency="EUR",
        verbose_name=_("Unit price"),
    )

    bom_rule = models.ForeignKey(
        'BOMRule',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="configuration_items",
        verbose_name=_("BOM rule"),
    )

    production_stages = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Production stages inherited from BOM rule"),
        verbose_name=_("Production stages"),
    )

    stage_resources = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Stage resources"),
        help_text=_("Snapshot of resources needed per stage for this item."),
    )

    label = models.CharField(
        max_length=255,
        editable=False,
        db_index=True,
        verbose_name=_("Label"),
        help_text=_("Snapshot label for this configuration item"),
    )

    note = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Note"),
    )

    def save(self, *args, **kwargs):
        quantity = self.quantity or Decimal("0")
        length = self.length or Decimal("1")

        self.line_total = self.unit_price * quantity * length

        if self.component:
            self.label = f"{self.component.name} × {quantity}"
        else:
            self.label = _("Unknown component")

        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if not self.is_template_item and self.production_stages:
            company = self.configuration.company
            if company:
                company_stages = set(company.production_stages or [])
                item_stages = set(self.production_stages)
                if not item_stages.issubset(company_stages):
                    raise ValidationError(
                        {
                            "production_stages": _(
                                "Configuration item stages must be a subset of the company production stages."
                            )
                        }
                    )

    def __str__(self):
        return self.label


class PricingRuleAssignment(models.Model):
    """
    Assigns a reusable PricingRuleTemplate to a configuration.
    Optional: allow override or enable/disable per configuration.
    """

    configuration = models.ForeignKey(
        Configuration, on_delete=models.CASCADE, related_name="pricing_rules"
    )

    rule = models.ForeignKey(
        PricingRule, on_delete=models.CASCADE, related_name="assignments"
    )

    enabled = models.BooleanField(default=True)

    class Meta:
        unique_together = ("configuration", "rule")

    def __str__(self):
        return f"{self.configuration_id} → {self.rule}"


class ProductPricingRuleAssignment(models.Model):
    product = models.ForeignKey(
        ProductTemplate, on_delete=models.CASCADE, related_name="pricing_rule_defaults"
    )
    rule = models.ForeignKey(
        PricingRule, on_delete=models.CASCADE, related_name="product_defaults"
    )
    enabled = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ("product", "rule")
        ordering = ["order"]


class ConfigurationParameterValue(models.Model):
    configuration = models.ForeignKey(
        Configuration, on_delete=models.CASCADE, related_name="param_values"
    )

    parameter = models.ForeignKey(
        ProductParameter, on_delete=models.CASCADE, related_name="values"
    )

    value = models.CharField(max_length=200)

    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Sorting Order"),
        help_text=_("Order of the parameter within the product"),
        db_index=True,
    )

    class Meta:
        ordering = ["order", "configuration"]
        indexes = [
            models.Index(
                fields=["configuration", "order"],
            )
        ]

    def __str__(self):
        return f"{self.parameter.name} = {self.value}"


class ProductComputedVariable(CompanyOwnedModel):
    product = models.ForeignKey(
        ProductTemplate, on_delete=models.CASCADE, related_name="computed_vars"
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
            raise ValidationError({"expression": "Expression cannot be empty."})

        try:
            validate_expression_for_safe_eval(expr)
        except ValueError as e:
            raise ValidationError({"expression": f"Invalid expression: {e}"})


class ProductComputedTemplate(CompanyOwnedModel):
    product = models.ForeignKey(
        ProductTemplate, on_delete=models.CASCADE, related_name="computed_templates"
    )
    name = models.CharField(max_length=100)
    condition_expression = models.TextField(
        blank=True,
        null=True,
        help_text="Condition to evaluate. If true, this template is selected. Example: width > 50"
    )
    template = models.TextField(
        help_text="HTML or SVG code to be injected"
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["product", "name", "order"]

    def __str__(self):
        return f"{self.product_id}.{self.name}"

    def clean(self):
        super().clean()

        expr = (self.condition_expression or "").strip()
        if expr:
            try:
                validate_expression_for_safe_eval(expr)
            except ValueError as e:
                raise ValidationError({"condition_expression": f"Invalid expression: {e}"})


class ExportTemplateKind(models.TextChoices):
    QUOTATION = "quotation", _("Quotation")
    PRODUCTION_ORDER = "production_order", _("Production Order")
    PRODUCTION_TICKET = "production_ticket", _("Production Ticket")
    SALES_INVOICE = "sales_invoice", _("Sales Invoice")
    CONSOLIDATED_QUOTATION = "consolidated_quotation", _("Consolidated Quotation")
    RECEIVAL_SLIP = "receival_slip", _("Receival Slip")
    ASSET_TRANSACTION = "asset_transaction", _("Asset Transaction")
    DELIVERY_ORDER = "delivery_order", _("Delivery Order")
    REPAIR_ORDER = "repair_order", _("Repair Order")
    QUOTE_REQUEST = "quote_request", _("Quote Request")
    PURCHASE_ORDER = "purchase_order", _("Purchase Order")
    AGENT_REPORT = "agent_report", _("Agent report")


class ExportTemplateSubtype(models.TextChoices):
    DOCUMENT = "document", _("Document")
    EMAIL = "email", _("Email")


class ConfigurationExportTemplate(CompanyOwnedModel):
    kind = models.CharField(
        max_length=30,
        choices=ExportTemplateKind.choices,
        default=ExportTemplateKind.QUOTATION,
        verbose_name=_("Kind"),
    )
    subtype = models.CharField(
        max_length=30,
        choices=ExportTemplateSubtype.choices,
        default=ExportTemplateSubtype.DOCUMENT,
        verbose_name=_("Subtype"),
        help_text=_("Which copy to use when several templates share the same kind."),
    )
    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="export_templates",
        null=True,  # If null → can be used by any product
        blank=True,
    )

    name = models.CharField(max_length=200)
    html = models.TextField(
        help_text="Django template syntax is allowed. Example: {{ config.total_price }}"
    )

    css = models.TextField(
        blank=True, help_text="CSS styling. Will be injected inside <style>...</style>"
    )

    language = models.CharField(
        max_length=2,
        blank=True,
        default="",
        choices=DocumentLanguage.choices,
        verbose_name=_("Language"),
        help_text=_(
            "Language this template is written in. Clients with this language get it; "
            "a template without one serves every client and prints in the client's language."
        ),
    )

    is_active = models.BooleanField(default=True)

    @classmethod
    def get_default_for_product(cls, product, kind, company=None, subtype=None, language=None):
        """
        Look up the most specific default template for a given product, kind and company.
        A template in ``language`` (default Spanish) wins, then one in no
        language; templates in other languages are left out. Then prioritizes
        templates explicitly linked to the product and the company.
        Pass subtype to distinguish templates that share a kind.
        """
        language = language or DocumentLanguage.ES
        templates = cls.objects.filter(
            Q(product=product) | Q(product__isnull=True),
            kind=kind,
            is_active=True,
            language__in=[language, ""],
        )
        if subtype is not None:
            templates = templates.filter(subtype=subtype)
        
        if company:
            templates = templates.filter(Q(company=company) | Q(company__isnull=True))
        else:
            templates = templates.filter(company__isnull=True)
            
        return templates.order_by(
            Case(When(language=language, then=Value(0)), default=Value(1)),
            F('product_id').desc(nulls_last=True),
            F('company_id').desc(nulls_last=True),
        ).first()

    def __str__(self):
        return f"{self.name} (Product {self.product_id or 'Any'})"


class ConfigurationComponentGroup(models.Model):
    configuration = models.ForeignKey(
        Configuration,
        on_delete=models.CASCADE,
        related_name="component_groups",
    )

    key = models.SlugField(max_length=50)
    label = models.CharField(max_length=100)
    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Sorting Order"),
        help_text=_("Order of the parameter within the product"),
        db_index=True,
    )

    items = models.ManyToManyField(
        ConfigurationItem,
        related_name="groups",
    )

    material_total = MoneyField(
        max_digits=14,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        editable=False,
    )

    pricing_adjustment = MoneyField(
        max_digits=14,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        editable=False,
    )

    total = MoneyField(
        max_digits=14,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        editable=False,
    )

    class Meta:
        unique_together = ("configuration", "key")

    def __str__(self):
        return self.label

    @property
    def price_total(self):
        """Sum of (unit_price × quantity) for every item in this group."""
        total = Decimal("0")
        for item in self.items.all():
            qty = item.quantity or Decimal("0")
            price = item.unit_price.amount if item.unit_price else Decimal("0")
            total += price * qty
        return total


class ReceivalSlip(Timestampable, CompanyOwnedModel):
    """
    Albarán de Recibida: Document tracking items received for a configuration.
    """
    configuration = models.ForeignKey(
        Configuration,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receival_slips",
        verbose_name=_("Configuration"),
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name=_("Reference"),
        help_text=_("Auto-generated internal reference (e.g., ALB-RX-00001)")
    )
    date = models.DateField(
        default=timezone.now,
        verbose_name=_("Date")
    )
    notes = models.TextField(
        blank=True,
        verbose_name=_("General Notes")
    )

    class Meta:
        verbose_name = _("Receival Slip")
        verbose_name_plural = _("Receival Slips")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'reference'],
                name='unique_receival_reference_per_company'
            )
        ]

    def __str__(self):
        return f"{self.reference or f'#{self.pk}'} ({self.date})"


class ReceivalSlipItem(models.Model):
    receival_slip = models.ForeignKey(
        ReceivalSlip,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Receival Slip"),
    )
    description = models.CharField(
        max_length=255,
        verbose_name=_("Item Name/Description")
    )
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name=_("Quantity")
    )
    measurements = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Measurements")
    )
    notes = models.TextField(
        blank=True,
        verbose_name=_("Item Notes")
    )

    class Meta:
        verbose_name = _("Receival Slip Item")
        verbose_name_plural = _("Receival Slip Items")

    def __str__(self):
        return f"{self.description} ({self.quantity})"


class ConfigurationPreset(CompanyOwnedModel):
    """
    A pre-made set of parameter values for a ProductTemplate.
    Admins create presets; users click "Use Preset" to instantly
    create a Configuration with these values pre-filled.
    """

    product = models.ForeignKey(
        ProductTemplate,
        on_delete=models.CASCADE,
        related_name="presets",
        verbose_name=_("Product"),
    )
    name = models.CharField(
        max_length=200,
        verbose_name=_("Name"),
    )
    reference_code = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Reference Code"),
        help_text=_("Optional external reference / catalog code."),
        db_index=True
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
    )
    parameter_values = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Map of parameter name → default value"),
        verbose_name=_("Parameter Values"),
    )
    image = models.ImageField(
        upload_to="presets/",
        blank=True,
        null=True,
        verbose_name=_("Image"),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Is Active"),
    )
    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Order"),
    )
    final_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        default_currency="EUR",
        verbose_name=_("Final Price"),
        help_text=_("If set, overrides the calculated subtotal price on the resulting configuration."),
    )

    class Meta:
        ordering = ["order", "name"]
        verbose_name = _("Configuration Preset")
        verbose_name_plural = _("Configuration Presets")

    def __str__(self):
        return f"{self.name} ({self.product.name})"

    def get_missing_parameters(self):
        """
        Returns required ProductParameter objects whose name
        is NOT present in this preset's parameter_values dict.
        """
        all_params = self.product.parameters.filter(required=True)
        filled = set(self.parameter_values.keys())
        return [p for p in all_params if p.name not in filled]
