from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from timestamps.models import Timestampable
from django.contrib.postgres.indexes import GinIndex
from company.models import Employee
from django.utils import timezone
from django.db.models import Q
from django.core.exceptions import ValidationError
from materials.models import Component

class ProductionTicketStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionTicket(Timestampable):
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

class ProductionItemStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In Progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionItem(Timestampable):
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

    class Meta:
        indexes = [
            GinIndex(fields=["planning"]),
        ]

class ProductionItemSessionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_PROGRESS = "in_progress", _("In progress")
    DONE = "done", _("Done")
    CANCELLED = "cancelled", _("Cancelled")

class ProductionItemSession(Timestampable):
    employee = models.ForeignKey(Employee,on_delete=models.CASCADE,related_name="production_sessions",
                                 verbose_name=_("Employee"))
    production_item = models.ForeignKey(
        ProductionItem,
        on_delete=models.CASCADE,
        related_name="sessions",
        verbose_name=_("Production Item"),
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
            models.Index(fields=["employee", "status"]),
        ]
        constraints = [models.UniqueConstraint(
                fields=["production_item", "production_stage", "employee"],
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
        self.full_clean()
        super().save(*args, **kwargs)


        
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