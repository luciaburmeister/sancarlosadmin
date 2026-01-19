from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from timestamps.models import Timestampable
from django.contrib.postgres.indexes import GinIndex

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

    class Meta:
        indexes = [
            GinIndex(fields=["planning"]),
        ]

class ProductionStageItem(ProductionItem):

    class Meta:
        proxy = True
        verbose_name = _("Production stage")
        verbose_name_plural = _("Production stages")

