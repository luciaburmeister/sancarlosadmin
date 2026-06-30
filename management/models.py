from decimal import Decimal

from django.db import models
from django.db.models import Sum, Q
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
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
    start_date = models.DateField(null=True, blank=True, verbose_name=_("Start Date"))
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

        invoiceable = Decimal(str(quote_subtotal)) + Decimal(str(config_subtotal))

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
    """

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
