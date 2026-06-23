from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords
from sales.models import Client
from company.models import CompanyOwnedModel, WorkingZone, BaseDocumentType, BaseDocument
from django.core.exceptions import ValidationError
from timestamps.models import Timestampable
from django.utils import timezone
from django.contrib.auth import get_user_model
from materials.models import Provider
User = get_user_model()


class AssetType(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    schema = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("JSON Schema to validate asset data (optional)")
    )

    class Meta:
        verbose_name = _("Asset type")
        verbose_name_plural = _("Asset types")
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name","company"]),
        ]

    def __str__(self):
        return self.name


class Asset(CompanyOwnedModel):
    asset_type = models.ForeignKey(
        AssetType,
        on_delete=models.PROTECT,
        related_name="assets",
        verbose_name=_("Asset Type")
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("Owner/Client")
    )
    name = models.CharField(max_length=200, verbose_name=_("Name/Label"))
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Flexible Data"),
        help_text=_("Schema-less data for this specific asset type.")
    )
    current_working_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_assets",
        verbose_name=_("Current Working Zone")
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("Provider")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    assigned_to = models.ForeignKey(
        'company.Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_assets",
        verbose_name=_("Assigned To")
    )

    history = HistoricalRecords()

    def save(self, *args, **kwargs):
        if self.data:
            self.data = {
                k.strip().lower().replace(" ", "_"): v
                for k, v in self.data.items()
            }
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = _("Asset")
        verbose_name_plural = _("Assets")

    def __str__(self):
        return f"{self.name} ({self.asset_type.name})"


class AssetIdentifier(models.Model):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        related_name="identifiers",
        verbose_name=_("Asset")
    )
    label = models.CharField(
        max_length=50,
        verbose_name=_("Label"),
        help_text=_("e.g., VIN, Plate Number, SN")
    )
    value = models.CharField(
        max_length=100,
        verbose_name=_("Value"),
        db_index=True
    )

    class Meta:
        verbose_name = _("Asset identifier")
        verbose_name_plural = _("Asset identifiers")

    def __str__(self):
        return f"{self.label}: {self.value}"

    def clean(self):
        super().clean()
        if self.asset and self.asset.company:
            labels = self.asset.company.asset_identifier_labels
            if labels and self.label not in labels:
                raise ValidationError({
                    'label': _("This label is not allowed for this company. Allowed: %(labels)s") % {'labels': ", ".join(labels)}
                })


class TransactionTypeChoices(models.TextChoices):
    DELIVERY = "delivery", _("Delivery")
    RETURN = "return", _("Return")
    MOVEMENT = "movement", _("Movement")


class AssetTransaction(CompanyOwnedModel, Timestampable):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        related_name="transactions",
        verbose_name=_("Asset")
    )
    from_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions_from",
        verbose_name=_("From Zone")
    )
    to_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions_to",
        verbose_name=_("To Zone")
    )
    transaction_type = models.CharField(
        max_length=10,
        choices=TransactionTypeChoices.choices,
        verbose_name=_("Transaction Type")
    )
    date = models.DateTimeField(default=timezone.now, verbose_name=_("Date"))
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Flexible Data"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    firma = models.TextField(blank=True, null=True, verbose_name=_("Signature"))
    firma_image = models.ImageField(
        upload_to="asset_transactions/firmas/",
        blank=True,
        null=True,
        verbose_name=_("Signature Image")
    )
    imagen = models.ImageField(
        upload_to="asset_transactions/images/",
        blank=True,
        null=True,
        verbose_name=_("Image")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="asset_transactions",
        verbose_name=_("Project File"),
    )
    performed_by = models.ForeignKey(
        "company.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="performed_transactions"
    )

    class Meta:
        verbose_name = _("Asset transaction")
        verbose_name_plural = _("Asset transactions")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.asset} ({self.from_zone} -> {self.to_zone})"


class AssetPlanning(CompanyOwnedModel, Timestampable):
    STATUS_CHOICES = [
        ('pending', _('Pending')),
        ('in_transit', _('In Transit')),
        ('delivered', _('Delivered')),
        ('cancelled', _('Cancelled')),
    ]

    PLANNING_CHOICES = [
        ('cuba', _('Container Movement')),
        ('porte', _('Transport')),
    ]

    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="plannings",
        verbose_name=_("Asset")
    )
    origin = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="plannings_origin",
        verbose_name=_("From Zone")
    )
    destination = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="plannings_destination",
        verbose_name=_("To Zone")
    )
    assigned_to = models.ForeignKey(
        'company.Employee',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="asset_plannings",
        verbose_name=_("Assigned To")
    )
    transport_asset = models.ForeignKey(
        Asset,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="transport_plannings",
        verbose_name=_("Transport Asset"),
        help_text=_("Vehicle or tool needed to perform this movement"),
    )
    transport_asset = models.ForeignKey(
        Asset,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="transport_plannings",
        verbose_name=_("Transport Asset"),
        help_text=_("Vehicle or tool needed to perform this movement"),
    )
    scheduled_date = models.DateField(verbose_name=_("Scheduled Date"))
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        verbose_name=_("Status")
    )
    route_order = models.PositiveSmallIntegerField(
        null=True, blank=True,
        verbose_name=_("Route Order"),
        help_text=_("Execution order within the employee's daily route"),
    )
    planning_type = models.CharField(
        max_length=10,
        choices=PLANNING_CHOICES,
        default='cuba',
        verbose_name=_("Planning Type")
    )
    load_description = models.TextField(
        blank=True,
        verbose_name=_("Load Description"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset Planning")
        verbose_name_plural = _("Asset Plannings")
        ordering = ["-scheduled_date"]
        indexes = [
            models.Index(fields=["assigned_to", "scheduled_date"]),
        ]
        indexes = [
            models.Index(fields=["assigned_to", "scheduled_date"]),
        ]

    def __str__(self):
        return f"{self.asset.name} → {self.destination} ({self.scheduled_date})"




class ZoneDistanceCache(models.Model):
    """Cache Google Routes API distances between zone pairs."""
    from_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("From Zone"),
    )
    to_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("To Zone"),
    )
    distance_meters = models.PositiveIntegerField(verbose_name=_("Distance (m)"))
    duration_seconds = models.PositiveIntegerField(verbose_name=_("Duration (s)"))
    fetched_at = models.DateTimeField(auto_now=True, verbose_name=_("Fetched At"))

    class Meta:
        verbose_name = _("Zone Distance Cache")
        verbose_name_plural = _("Zone Distance Cache")
        constraints = [
            models.UniqueConstraint(
                fields=["from_zone", "to_zone"],
                name="unique_zone_pair",
            ),
        ]

    def __str__(self):
        return f"{self.from_zone} → {self.to_zone}: {self.distance_meters}m"


class AssetPlanningStop(models.Model):
    """Ordered intermediate stop between origin and destination."""
    planning = models.ForeignKey(
        AssetPlanning,
        on_delete=models.CASCADE,
        related_name="stops",
        verbose_name=_("Planning"),
    )
    zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.PROTECT,
        verbose_name=_("Zone"),
    )


    sequence = models.PositiveSmallIntegerField(
        verbose_name=_("Sequence"),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Planning Stop")
        verbose_name_plural = _("Planning Stops")
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["planning", "sequence"],
                name="unique_stop_sequence",
            ),
        ]

    def __str__(self):
        return f"Stop {self.sequence}: {self.zone}"


class AssetDocumentType(BaseDocumentType):
    class Meta:
        verbose_name = _("Asset Document Type")
        verbose_name_plural = _("Asset Document Types")


class AssetDocument(BaseDocument):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("Asset")
    )
    document_type = models.ForeignKey(
        AssetDocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name=_("Document Type")
    )

    class Meta:
        verbose_name = _("Asset Document")
        verbose_name_plural = _("Asset Documents")

    def __str__(self):
        return f"{self.document_type.name} - {self.asset}"

    def save(self, *args, **kwargs):
        if not self.company_id and self.asset:
            self.company = self.asset.company
        super().save(*args, **kwargs)