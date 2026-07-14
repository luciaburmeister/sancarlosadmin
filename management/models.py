from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from djmoney.models.fields import MoneyField
from company.models import CompanyOwnedModel
from timestamps.models import Timestampable
from sales.models import Client
from management.services import ProjectFileService

class ProjectFileStatus(models.TextChoices):
    NEW = "new", _("New")
    ACTIVE = "active", _("Active")
    ON_HOLD = "on_hold", _("On Hold")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")

class ProjectFile(Timestampable, CompanyOwnedModel):
    """
    Expediente: The central hub for a project's lifecycle.
    """
    reference = models.CharField(
        max_length=50, 
        unique=False,  
        blank=True, 
        null=True,
        verbose_name=_("Reference"),
        help_text=_("Auto-generated internal reference (e.g., EXP-00001)")
    )
    incremental_id = models.PositiveIntegerField(
        db_index=True, 
        blank=True, 
        null=True,
        verbose_name=_("Incremental ID")
    )
    name = models.CharField(
        max_length=255, 
        verbose_name=_("Project Name"),
        help_text=_("Project or site name")
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        related_name="project_files",
        verbose_name=_("Client")
    )
    status = models.CharField(
        max_length=20,
        choices=ProjectFileStatus.choices,
        default=ProjectFileStatus.NEW,
        verbose_name=_("Status")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    start_date = models.DateField(null=True, blank=True, verbose_name=_("Start Date"), default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True, verbose_name=_("End Date"))
    notification_email = models.EmailField(
        blank=True,
        verbose_name=_("Notification Email"),
        help_text=_("Email address for project notifications (e.g., asset transactions). Falls back to the client email if left empty."),
    )

    total_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Amount"),
        help_text=_("Cached total of accepted quotes + direct configurations."),
    )

    total_invoiced = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Invoiced"),
        help_text=_("Cached sum of sales invoices linked to this project file."),
    )

    percentage_invoiced = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("% Invoiced"),
        help_text=_("Cached percentage of total_amount that has been invoiced."),
    )

    class Meta:
        verbose_name = _("Project File")
        verbose_name_plural = _("Project Files")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'reference'], 
                name='unique_reference_per_company'
            )
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            ref_data = ProjectFileService.generate_next_reference_data(self.company)
            self.reference = ref_data['reference']
            self.incremental_id = ref_data['incremental_id']
        super().save(*args, **kwargs)

    def __str__(self):
        ref = self.reference or f"#{self.pk}"
        return f"[{ref}] {self.name} - {self.client}"

    def recalculate_total_amount(self):
        """
        Recompute total_amount, total_invoiced, and percentage_invoiced.
        All values are subtotals (pre-tax) so the comparison is apples-to-apples.
        Call this whenever a related Quote, Configuration, or SalesInvoice changes.
        """
        from django.apps import apps
        Quote = apps.get_model("sales", "Quote")
        Configuration = apps.get_model("configurator", "Configuration")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")

        # Accepted quotes subtotal (pre-tax, post-discount)
        quote_subtotal = (
            Quote.objects.filter(
                project_file=self,
                status="accepted",
            ).aggregate(total=Sum("discounted_subtotal"))["total"]
            or Decimal("0.00")
        )
        if hasattr(quote_subtotal, "amount"):
            quote_subtotal = quote_subtotal.amount

        # Direct configurations (no quote) subtotal (pre-tax)
        config_subtotal = (
            Configuration.objects.filter(
                project_file=self,
                quote__isnull=True,
                status__in=["shop", "completed", "converted", "production_done"],
            ).aggregate(total=Sum("subtotal_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(config_subtotal, "amount"):
            config_subtotal = config_subtotal.amount

        # Work entries subtotal
        work_entry_subtotal = (
            self.work_entries.aggregate(total=Sum("total_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(work_entry_subtotal, "amount"):
            work_entry_subtotal = work_entry_subtotal.amount

        invoiceable = Decimal(str(quote_subtotal)) + Decimal(str(config_subtotal)) + Decimal(str(work_entry_subtotal))

        # Sum of sales invoice subtotals (pre-tax)
        # Source 1: single-PF invoices (project_file FK on invoice)
        from django.db.models import Q
        SalesInvoiceItem = apps.get_model("invoices", "SalesInvoiceItem")

        fk_invoiced = (
            SalesInvoice.objects.filter(
                project_file=self,
            ).aggregate(total=Sum("subtotal_amount"))["total"]
            or Decimal("0.00")
        )
        if hasattr(fk_invoiced, "amount"):
            fk_invoiced = fk_invoiced.amount

        # Source 2: bulk invoices (project_file FK on individual items)
        item_invoiced = (
            SalesInvoiceItem.objects.filter(
                project_file=self,
                invoice__project_file__isnull=True,  # only bulk invoices
            ).aggregate(total=Sum("line_total"))["total"]
            or Decimal("0.00")
        )
        if hasattr(item_invoiced, "amount"):
            item_invoiced = item_invoiced.amount

        invoiced = Decimal(str(fk_invoiced)) + Decimal(str(item_invoiced))

        pct = (
            (invoiced / invoiceable * Decimal("100")).quantize(Decimal("0.01"))
            if invoiceable > 0 else Decimal("0.00")
        )

        self.total_amount = invoiceable
        self.total_invoiced = invoiced
        self.percentage_invoiced = pct
        self.save(update_fields=[
            "total_amount", "total_amount_currency",
            "total_invoiced", "total_invoiced_currency",
            "percentage_invoiced",
        ])


class ResourcePlanningType(CompanyOwnedModel):
    """
    Company-configurable planning categories.
    Examples: "Machine Work", "Maintenance", "Installation", "Transport"
    """
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        verbose_name=_("Color"),
        help_text=_("Hex color for calendar display, e.g. #3B82F6"),
    )
    icon = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Icon"),
        help_text=_("Material icon name, e.g. 'build', 'local_shipping'"),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Resource Planning Type")
        verbose_name_plural = _("Resource Planning Types")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                name="unique_planning_type_per_company",
            ),
        ]

    def __str__(self):
        return self.name


class ResourcePlanningStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    CONFIRMED = "confirmed", _("Confirmed")
    IN_PROGRESS = "in_progress", _("In Progress")
    COMPLETED = "completed", _("Completed")
    CANCELLED = "cancelled", _("Cancelled")


class ResourcePlanningGroup(Timestampable, CompanyOwnedModel):
    """
    Optional grouping for ResourcePlanning records created together.
    Holds no metadata — the group is just an identifier (its PK) that
    lets the admin update or cancel all members in a single action.
    Repetition series (calendar-style repeats) are marked with
    is_repetition: their members have different dates, so they are edited
    through the series editor instead of the regular group form.
    """

    is_repetition = models.BooleanField(default=False, verbose_name=_("Repetition series"))

    class Meta:
        verbose_name = _("Resource Planning Group")
        verbose_name_plural = _("Resource Planning Groups")

    def __str__(self):
        return f"Group #{self.pk}"

    def cancel_all(self) -> None:
        self.plannings.update(status=ResourcePlanningStatus.CANCELLED)


class ResourcePlanning(Timestampable, CompanyOwnedModel):
    """
    Generic resource planning: assign any resource (Employee, Machine, Asset…)
    to a ProjectFile for a specific date/time window.
    """
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="resource_plannings",
        verbose_name=_("Project File"),
    )

    planning_type = models.ForeignKey(
        ResourcePlanningType,
        on_delete=models.PROTECT,
        related_name="plannings",
        verbose_name=_("Planning Type"),
    )

    group = models.ForeignKey(
        ResourcePlanningGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="plannings",
        verbose_name=_("Group"),
    )

    # Generic FK → the resource being planned
    resource_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        verbose_name=_("Resource Type"),
    )
    resource_object_id = models.PositiveBigIntegerField(
        verbose_name=_("Resource ID"),
    )
    resource = GenericForeignKey("resource_content_type", "resource_object_id")

    # Schedule
    start = models.DateTimeField(verbose_name=_("Start"))
    end = models.DateTimeField(verbose_name=_("End"))
    exclude_weekends = models.BooleanField(default=False, verbose_name=_("Exclude weekends"))

    status = models.CharField(
        max_length=20,
        choices=ResourcePlanningStatus.choices,
        default=ResourcePlanningStatus.PLANNED,
        verbose_name=_("Status"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    show_planning_type = models.BooleanField(
        default=False,
        verbose_name=_("Show planning type"),
        help_text=_("Label this planning with its planning type instead of the project file name in the schedule view."),
    )

    class Meta:
        verbose_name = _("Resource Planning")
        verbose_name_plural = _("Resource Plannings")
        ordering = ["start"]
        indexes = [
            models.Index(
                fields=["resource_content_type", "resource_object_id"],
            ),
            models.Index(fields=["start", "end"]),
            models.Index(fields=["project_file", "start"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(end__gt=models.F("start")),
                name="resource_planning_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.resource} → {self.project_file} ({self.start:%d/%m %H:%M}–{self.end:%H:%M})"

    def cancel(self) -> None:
        self.status = ResourcePlanningStatus.CANCELLED
        self.save(update_fields=["status"])

    def clean(self):
        super().clean()
        if self.start and self.end and self.end <= self.start:
            raise ValidationError({
                "end": _("End must be after start."),
            })

        # Warn about overlaps for the same resource
        if self.resource_content_type_id and self.resource_object_id and self.start and self.end:
            overlaps = ResourcePlanning.objects.filter(
                resource_content_type=self.resource_content_type,
                resource_object_id=self.resource_object_id,
                start__lt=self.end,
                end__gt=self.start,
            ).exclude(
                pk=self.pk,
            ).exclude(
                status=ResourcePlanningStatus.CANCELLED,
            )
            if overlaps.exists():
                raise ValidationError(
                    _("This resource already has a planning that overlaps with the selected time range.")
                )


class ResourcePlanningBacklogStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SCHEDULED = "scheduled", _("Scheduled")
    CANCELLED = "cancelled", _("Cancelled")


class ResourcePlanningBacklogItem(Timestampable, CompanyOwnedModel):
    """
    A planning prepared without dates or resources, waiting to be scheduled.
    Scheduling one creates ResourcePlanning records through the regular bulk
    form and marks the item as scheduled. If those plannings are later
    cancelled the item does NOT revive automatically — use the manual
    "Reopen" admin action instead.
    """

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="planning_backlog_items",
        verbose_name=_("Project File"),
    )
    planning_type = models.ForeignKey(
        ResourcePlanningType,
        on_delete=models.PROTECT,
        related_name="backlog_items",
        verbose_name=_("Planning Type"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    show_planning_type = models.BooleanField(
        default=False,
        verbose_name=_("Show planning type"),
        help_text=_("Label this planning with its planning type instead of the project file name in the schedule view."),
    )
    estimated_days = models.PositiveSmallIntegerField(
        default=1,
        verbose_name=_("Estimated days"),
        help_text=_("Working days; used to suggest the date range when scheduling."),
    )
    daily_start = models.TimeField(null=True, blank=True, verbose_name=_("Daily start"))
    daily_end = models.TimeField(null=True, blank=True, verbose_name=_("Daily end"))
    exclude_weekends = models.BooleanField(default=True, verbose_name=_("Exclude weekends"))
    priority = models.PositiveIntegerField(
        default=100,
        db_index=True,
        verbose_name=_("Priority"),
        help_text=_("Lower numbers appear first in the backlog."),
    )
    is_reusable = models.BooleanField(
        default=False,
        verbose_name=_("Reusable"),
        help_text=_("Keep this item in the backlog after scheduling so it can be planned again."),
    )
    status = models.CharField(
        max_length=20,
        choices=ResourcePlanningBacklogStatus.choices,
        default=ResourcePlanningBacklogStatus.PENDING,
        verbose_name=_("Status"),
    )
    scheduled_group = models.ForeignKey(
        ResourcePlanningGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="backlog_items",
        verbose_name=_("Scheduled group"),
    )
    scheduled_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Scheduled at"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("Created by"),
    )

    class Meta:
        verbose_name = _("Planning Backlog Item")
        verbose_name_plural = _("Planning Backlog")
        ordering = ["priority", "created_at"]
        indexes = [
            models.Index(fields=["company", "status", "priority"]),
        ]

    def save(self, *args, **kwargs):
        # Inline creation from the ProjectFile admin does not set company;
        # inherit it from the expediente so company-scoped queries see the item.
        if not self.company_id and self.project_file_id:
            self.company_id = self.project_file.company_id
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.project_file} · {self.planning_type} ({self.get_status_display()})"


# ---------------------------------------------------------------------------
# Work Entries
# ---------------------------------------------------------------------------


class WorkEntryCategory(CompanyOwnedModel):
    """
    Catalog of work entry categories (e.g. Puesta, Recogida, Transporte).
    Used to group WorkEntryTypes so project-file-scoped overrides can
    replace company-wide defaults of the same category.
    """

    name = models.CharField(
        max_length=200,
        verbose_name=_("Name"),
    )
    slug = models.SlugField(
        max_length=100,
        verbose_name=_("Slug"),
        help_text=_("Unique identifier for this category within the company."),
    )
    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
    )

    class Meta:
        verbose_name = _("Work Entry Category")
        verbose_name_plural = _("Work Entry Categories")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "slug"],
                name="unique_work_entry_category_per_company",
            ),
        ]

    def __str__(self):
        return self.name


class WorkEntryType(CompanyOwnedModel):
    """
    Configurable template defining a category of billable work and how to price it.
    Can be company-wide (project_file=NULL) or scoped to a specific Expediente.
    When multiple types share the same category, the project-file-scoped one
    takes priority over the company-wide default.
    """

    category = models.ForeignKey(
        WorkEntryCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="work_entry_types",
        verbose_name=_("Category"),
        help_text=_("Groups this type so project-file overrides replace company-wide defaults of the same category."),
    )
    name = models.CharField(max_length=200, verbose_name=_("Name"))
    unit_label = models.CharField(
        max_length=50,
        default="unit",
        verbose_name=_("Unit Label"),
        help_text=_("e.g. trip, hour, km, unit"),
    )

    applicability_expression = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Applicability Expression"),
        help_text=_(
            "safe_eval condition evaluated against the source object context. "
            "Determines if this type SHOULD be created for a given source. "
            "Leave empty to always apply.\n"
            "Example: transaction_type == 'delivery'"
        ),
    )

    description_template = models.CharField(
        max_length=500,
        blank=True,
        verbose_name=_("Description Template"),
        help_text=_(
            "Template for auto-generating the work entry description. "
            "Use {field_name} placeholders.\n"
            "Example: Delivery: {asset_name} → {to_zone}"
        ),
    )

    # Linked pricing data — only these feed into expression context
    materials = models.ManyToManyField(
        "materials.Material",
        blank=True,
        related_name="work_entry_types",
        verbose_name=_("Materials"),
        help_text=_("Materials whose pricing is available in expression context as materials.<rule_key>."),
    )
    components = models.ManyToManyField(
        "materials.Component",
        blank=True,
        related_name="work_entry_types",
        verbose_name=_("Components"),
        help_text=_("Components whose pricing is available in expression context as components.<slug>."),
    )

    source_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Source Model"),
        help_text=_("Restrict this type to a specific source model (e.g. Asset Transaction)."),
    )

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="custom_work_entry_types",
        verbose_name=_("Project File"),
        help_text=_("Leave empty for company-wide types. Set to restrict to a specific Expediente."),
    )

    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Work Entry Type")
        verbose_name_plural = _("Work Entry Types")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "category", "project_file"],
                name="unique_work_entry_type_per_company_category_project",
            ),
        ]

    def __str__(self):
        suffix = f" [{self.project_file.reference}]" if self.project_file else ""
        return f"{self.name}{suffix}"

    def evaluate_applicability(self, context: dict) -> bool:
        """Check if this type applies to a source given its context."""
        if not self.applicability_expression:
            return True
        from configurator.utils import safe_eval
        try:
            return bool(safe_eval(self.applicability_expression, context))
        except Exception:
            return False

    def compute_unit_price(self, context: dict) -> Decimal:
        """
        Execute pricing steps in order. Each step's output is added to the
        context for subsequent steps. Returns the last step's result,
        or Decimal('0.00') if no steps exist.

        If `context['components']` is a lazy accessor, it is kept in sync
        with the step_context so component price rules can use variables
        defined by earlier steps.
        """
        from configurator.utils import safe_eval

        steps = self.pricing_steps.all()
        if not steps.exists():
            return Decimal("0.00")

        step_context = dict(context)
        result = Decimal("0.00")

        # Get reference to lazy accessor (if present) to keep it in sync
        lazy_components = step_context.get("components")
        _has_bind = hasattr(lazy_components, "bind_context")

        for step in steps:
            try:
                if _has_bind:
                    lazy_components.bind_context(step_context)
                value = safe_eval(step.expression, step_context)
                step_context[step.variable_name] = value
                result = Decimal(str(value))
            except Exception:
                pass

        return result


class WorkEntryPricingStep(models.Model):
    """
    An ordered expression step in a WorkEntryType's pricing chain.
    Each step evaluates an expression and outputs a named variable
    available to subsequent steps. The last step's output becomes
    the unit_price.
    """

    entry_type = models.ForeignKey(
        WorkEntryType,
        on_delete=models.CASCADE,
        related_name="pricing_steps",
        verbose_name=_("Work Entry Type"),
    )

    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Order"),
        help_text=_("Execution order (lower = first)."),
        db_index=True
    )

    variable_name = models.SlugField(
        max_length=50,
        verbose_name=_("Variable Name"),
        help_text=_("Output variable name for this step, e.g. 'base_cost'. Available to subsequent steps."),
    )

    expression = models.TextField(
        verbose_name=_("Expression"),
        help_text=_(
            "safe_eval expression. Can use context variables (quantity, default_price, "
            "materials.*, components.*, source fields) plus variables from previous steps.\n"
            "Example: data.distance_km * 3.5"
        ),
    )

    class Meta:
        verbose_name = _("Pricing Step")
        verbose_name_plural = _("Pricing Steps")
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["entry_type", "variable_name"],
                name="unique_pricing_step_variable",
            ),
        ]

    def __str__(self):
        return f"#{self.order} {self.variable_name}"

    def clean(self):
        super().clean()
        from configurator.utils import validate_expression_for_safe_eval
        if self.expression:
            try:
                validate_expression_for_safe_eval(self.expression)
            except ValueError as e:
                raise ValidationError({"expression": str(e)})


class WorkEntry(Timestampable, CompanyOwnedModel):
    """
    A billable unit of work linked to a ProjectFile (Expediente).
    Can optionally reference a source object (e.g. AssetTransaction)
    via GenericFK. Multiple WorkEntries can point to the same source.
    """

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.CASCADE,
        related_name="work_entries",
        verbose_name=_("Project File"),
    )

    entry_type = models.ForeignKey(
        WorkEntryType,
        on_delete=models.PROTECT,
        related_name="entries",
        verbose_name=_("Work Entry Type"),
    )

    description = models.CharField(
        max_length=500,
        verbose_name=_("Description"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("1.00"),
        verbose_name=_("Quantity"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Unit Price"),
    )

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Price"),
        help_text=_("Auto-computed: quantity × unit_price."),
    )

    date = models.DateField(verbose_name=_("Date"))

    # GenericFK to any source object
    source_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Source Type"),
    )
    source_object_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Source ID"),
    )
    source = GenericForeignKey("source_content_type", "source_object_id")

    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    is_locked = models.BooleanField(
        default=False,
        verbose_name=_("Locked"),
        help_text=_("Locked after invoicing to prevent edits."),
    )

    applied_taxes = GenericRelation(
        "invoices.AppliedTax",
        content_type_field="content_type",
        object_id_field="object_id",
    )

    class Meta:
        verbose_name = _("Work Entry")
        verbose_name_plural = _("Work Entries")
        ordering = ["-date", "-created_at"]
        indexes = [
            models.Index(fields=["source_content_type", "source_object_id"]),
            models.Index(fields=["project_file", "date"]),
        ]

    def __str__(self):
        return f"{self.description} ({self.total_price})"

    @property
    def total_with_tax(self):
        """total_price + sum of all applied tax amounts."""
        from djmoney.money import Money
        tax_sum = Decimal("0")
        for tax in self.applied_taxes.all():
            if tax.calculated_amount is not None:
                amt = tax.calculated_amount.amount if isinstance(tax.calculated_amount, Money) else Decimal(str(tax.calculated_amount))
                tax_sum += amt
        base = self.total_price.amount if hasattr(self.total_price, 'amount') else Decimal(str(self.total_price or 0))
        currency = self.total_price.currency if hasattr(self.total_price, 'currency') else 'EUR'
        return Money(base + tax_sum, currency)

    def save(self, *args, **kwargs):
        # Auto-fill description from template if empty
        if not self.description and self.entry_type and self.entry_type.description_template:
            try:
                from management.services.work_entry import WorkEntryService
                context = WorkEntryService.build_source_context(self.source)
                self.description = self.entry_type.description_template.format(**context)
            except Exception:
                self.description = self.entry_type.name

        # Compute total
        self.total_price = self.unit_price * self.quantity
        super().save(*args, **kwargs)
