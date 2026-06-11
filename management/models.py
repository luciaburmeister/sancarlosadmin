from decimal import Decimal

from django.db import models
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _
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

    total_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Total Amount"),
        help_text=_("Cached total of accepted quotes + direct configurations."),
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
        Recompute total_amount from accepted quotes and direct configurations.
        Call this whenever a related Quote or Configuration changes.
        """
        from django.apps import apps
        Quote = apps.get_model("sales", "Quote")
        Configuration = apps.get_model("configurator", "Configuration")
        QuoteStatus = apps.get_model("sales", "QuoteStatus") if False else None

        # Accepted quotes total
        quote_total = (
            Quote.objects.filter(
                project_file=self,
                status="accepted",
            ).aggregate(total=Sum("total_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(quote_total, "amount"):
            quote_total = quote_total.amount

        # Direct configurations (no quote) in invoiceable statuses
        config_total = (
            Configuration.objects.filter(
                project_file=self,
                quote__isnull=True,
                status__in=["shop", "completed", "converted", "production_done"],
            ).aggregate(total=Sum("total_price"))["total"]
            or Decimal("0.00")
        )
        if hasattr(config_total, "amount"):
            config_total = config_total.amount

        self.total_amount = Decimal(str(quote_total)) + Decimal(str(config_total))
        self.save(update_fields=["total_amount", "total_amount_currency"])

