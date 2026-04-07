from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from timestamps.models import Timestampable
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.fields import ArrayField
from company.models import Employee,Machine,CompanyOwnedModel
from django.utils import timezone
from django.db.models import Q
from django.core.exceptions import ValidationError
from materials.models import Component


class ProductionTicketStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionTicket(Timestampable, CompanyOwnedModel):
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="production_tickets",
    )
    object_id = models.PositiveBigIntegerField()
    source_object = GenericForeignKey("content_type", "object_id")

    status = models.CharField(
        max_length=20,
        choices=ProductionTicketStatus.choices,
        default=ProductionTicketStatus.PENDING,
        db_index=True,
        verbose_name=_("Status")
    )

    context = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Company-specific context"),
        verbose_name=_("Contexto")
    )

    order_html = models.TextField(blank=True, null=True, verbose_name=_("Order HTML"))
    ticket_html = models.TextField(blank=True, null=True, verbose_name=_("Ticket HTML"))
    work_order_pdf = models.FileField(
        upload_to="production/work_orders/",
        null=True,
        blank=True,
        verbose_name=_("Work Order PDF")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="production_tickets",
        verbose_name=_("Project File")
    )


    def __str__(self):
        return str(self.pk)
    
    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_production_ticket_per_source"
            )
        ]

    def compute_status_from_items(self):
        items = list(self.items.all())
        if not items:
            return ProductionTicketStatus.PENDING

        stage_statuses = [
            ((info or {}).get("status") or ProductionItemStatus.PENDING)
            for item in items
            for info in (item.planning or {}).values()
        ]

        if not stage_statuses:
            return ProductionTicketStatus.PENDING

        if all(s == ProductionItemStatus.DONE for s in stage_statuses):
            return ProductionTicketStatus.DONE
        if any(s == ProductionItemStatus.IN_PROGRESS for s in stage_statuses):
            return ProductionTicketStatus.IN_PROGRESS
        if any(s == ProductionItemStatus.DONE for s in stage_statuses):
            return ProductionTicketStatus.IN_PROGRESS

        return ProductionTicketStatus.PENDING


    def recompute_status(self):
        new_status = self.compute_status_from_items()

        if new_status != self.status:
            ProductionTicket.objects.filter(pk=self.pk).update(status=new_status)
            self.status = new_status


class ProductionItemStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionItem(Timestampable, CompanyOwnedModel):
    ticket = models.ForeignKey(
        ProductionTicket,
        on_delete=models.CASCADE,
        related_name="items",
    )

    data = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Snapshot data (component, dimensions, notes)")
    )

    planning = models.JSONField(
        default=dict,
        help_text=_("Per-item production stages and progress"),
        verbose_name=_("Process Planning")
    )

    status = models.CharField(
        max_length=20,
        default=ProductionItemStatus.PENDING,
        choices=ProductionItemStatus.choices,
        verbose_name=_("Status")
    )

    component = models.ForeignKey(
        Component,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="production_items",
        verbose_name=_("Component"),
    )

    quantity = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Quantity"),
    )

    length = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Length"),
    )
    
    assigned_employees = models.ManyToManyField(
        Employee,
        blank=True,
        related_name="assigned_production_items",
        verbose_name=_("Assigned Employees")
    )

    ticket_html = models.TextField(blank=True, null=True, verbose_name=_("Ticket HTML"))

    production_stages_order = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        help_text=_("Stable stage order derived from BOM"),
        verbose_name=_("Production Stages Order"),
    )

    class Meta:
        indexes = [
            GinIndex(fields=["planning"]),
        ]

    def get_ordered_planning(self):
        """
        Returns the planning dictionary items (stage_name, info) sorted by 
        the production_stages_order if present, otherwise fallback to product template order.
        """
        planning = self.planning or {}
        
        # 1. Use internal order if available
        stage_order = None
        
        # 2. Fallback to product template order
        if not stage_order and self.ticket and self.ticket.source_object:
            product = getattr(self.ticket.source_object, "product", None)
            if product:
                stage_order = list(product.production_stages or [])

        def sort_key(item):
            stage_name = item[0]
            try:
                # Priority 0: Stages in the defined order
                return (0, stage_order.index(stage_name))
            except ValueError:
                # Priority 1: Other stages sorted alphabetically
                return (1, stage_name)

        return sorted(planning.items(), key=sort_key)
    
    def save(self, *args, **kwargs):
        if not self.company_id and self.ticket_id:
            self.company = self.ticket.company
        super().save(*args, **kwargs)
        self.ticket.recompute_status()


class ProductionItemSessionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")


class ProductionItemSessionKind(models.TextChoices):
    ACTIVITY_STARTED = "activity_started", _("Activity started")
    ACTIVITY_FINISHED = "activity_finished", _("Activity finished")
    SESSION_STARTED = "session_started", _("Session started")
    SESSION_FINISHED = "session_finished", _("Session finished")


class ProductionItemSession(Timestampable, CompanyOwnedModel):
    production_item = models.ForeignKey(
        ProductionItem,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Production Item"),
    )
    kind = models.CharField(
        max_length=32,
        choices=ProductionItemSessionKind.choices,
        null=True,
        blank=True,
        db_index=True,
        verbose_name=_("Type"),
    )
    start_time = models.DateTimeField(verbose_name=("Start Time"),null=True,blank=True)
    end_time = models.DateTimeField(verbose_name=_("End Time"),null=True,blank=True)
    comments = models.TextField(max_length=200,verbose_name=_("Comments"),null=True,blank=True)
    production_stage = models.CharField(max_length=25,db_index=True,verbose_name=_("Production Stage"))
    status = models.CharField(
        max_length=20,
        choices=ProductionItemSessionStatus.choices,
        default=ProductionItemSessionStatus.PENDING,
        db_index=True,
        verbose_name=_("Status")
    )
    created_by = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="created_sessions",
        null=True,
        blank=True,
        verbose_name=_("Created by")
    )
    completed_by = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="completed_sessions",
        null=True,
        blank=True,
        verbose_name=_("Completed by")
    )

    class Meta:
        indexes = [
            models.Index(fields=["production_item", "production_stage"]),
        ]
        constraints = [models.UniqueConstraint(
                fields=["production_item", "production_stage"],
                condition=Q(end_time__isnull=True),
                name="unique_open_session_per_employee_item_stage",
            )]
        
    def clean(self):
        if self.end_time and self.start_time and self.end_time < self.start_time:
            raise ValidationError(_("End time cannot be before start time"))

        if self.status == ProductionItemSessionStatus.IN_PROGRESS and not self.start_time:
            raise ValidationError(_("In-progress sessions must have a start time"))

        if self.status == ProductionItemSessionStatus.DONE:
            if not self.start_time or not self.end_time:
                raise ValidationError(_("Finished sessions must have start and end times"))
            
    def save(self, *args, **kwargs):
        if not self.company_id and self.production_item_id:
            self.company = self.production_item.company
        self.full_clean()
        super().save(*args, **kwargs)

        ticket = self.production_item.ticket
        ticket.recompute_status()

        
    @property
    def duration(self):
        if not self.end_time:
            return None
        return self.end_time - self.start_time

    def start(self, started_by: Employee | None = None):
        if self.status != ProductionItemSessionStatus.PENDING:
            raise ValidationError(_("Only pending sessions can be started"))
        if self.start_time:
            raise ValidationError(_("Session already started"))

        self.start_time = timezone.now()
        self.status = ProductionItemSessionStatus.IN_PROGRESS
        if started_by:
            self.created_by = started_by
        self.save()
    
    def finish(self, completed_by: Employee, comments=None):
        if self.status not in {
            ProductionItemSessionStatus.IN_PROGRESS,
            ProductionItemSessionStatus.PENDING,
        }:
            raise ValidationError(_("Only active sessions can be finished"))

        self.end_time = timezone.now()
        self.status = ProductionItemSessionStatus.DONE
        self.completed_by = completed_by
        if comments:
            self.comments = comments
        self.save()

class ProductionResource(Timestampable,CompanyOwnedModel):
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="production_resources",
    )
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    daily_capacity_minutes = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id"],
                name="unique_production_resource_per_target",
            )
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def clean(self):
        # Optional: whitelist allowed models
        if not isinstance(self.target, (Employee, Machine)):
            raise ValidationError(_("Unsupported resource type"))

    def __str__(self):
        return str(self.target)
    
class OperationRequirement(models.Model):
    production_item = models.ForeignKey(
        ProductionItem,
        on_delete=models.CASCADE,
        related_name="operation_requirements",
    )

    production_stage = models.CharField(max_length=25, db_index=True)

    required_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        help_text=_("Type of resource required (Employee, Machine, etc.)"),
    )

    quantity = models.PositiveSmallIntegerField(default=1)
    mandatory = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["production_item", "production_stage"]),
            models.Index(fields=["required_content_type"]),
        ]


class PlannedProductionSessionSource(models.TextChoices):
    AUTOMATIC= "auto",_("Automatic")
    MANUAL = "manual", _("Manual")

class PlannedProductionSession(Timestampable):
    production_item = models.ForeignKey(
        ProductionItem,
        on_delete=models.CASCADE,
        related_name="planned_sessions",
    )

    production_stage = models.CharField(
        max_length=25,
        db_index=True,
        verbose_name=_("Production Stage"),
    )

    planned_start = models.DateTimeField(db_index=True)
    planned_end = models.DateTimeField(db_index=True)

    primary_resource = models.ForeignKey(
        "ProductionResource",
        on_delete=models.PROTECT,
        related_name="planned_primary_sessions",
        null=True,
        blank=True,
    )

    locked = models.BooleanField(default=False,verbose_name=_("Locked"))
    source = models.CharField(
        max_length=20,
        choices=PlannedProductionSessionSource.choices,
        default="auto",
        verbose_name=_("Source")
    )
    notes = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["planned_start", "planned_end"]),
            models.Index(fields=["primary_resource"]),
        ]

class PlannedSessionResource(models.Model):
    planned_session = models.ForeignKey(
        PlannedProductionSession,
        on_delete=models.CASCADE,
        related_name="resources",
    )

    resource = models.ForeignKey(
        ProductionResource,
        on_delete=models.PROTECT,
        related_name="planned_assignments",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["planned_session", "resource"],
                name="unique_resource_per_planned_session",
            )
        ]
        indexes = [
            models.Index(fields=["resource"]),
        ]
