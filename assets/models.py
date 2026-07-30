from django.contrib.postgres.fields import ArrayField
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
from core.forms.dynamic_fields import json_schema_fragment
User = get_user_model()


class AssetType(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    schema = models.JSONField(default=dict, blank=True, help_text=_("JSON Schema to validate asset data (optional)"))
    identifier_labels = ArrayField(
        base_field=models.CharField(max_length=50),
        default=list,
        blank=True,
        verbose_name=_("Identifier labels"),
        help_text=_(
            "Identifier labels for this asset type (e.g. Matrícula, Bastidor). "
            "Leave empty to fall back to the company-wide labels."
        ),
    )

    class Meta:
        verbose_name = _("Asset type")
        verbose_name_plural = _("Asset types")
        ordering = ["name"]
        indexes = [models.Index(fields=["name", "company"])]

    def __str__(self):
        return self.name

    def get_identifier_labels(self):
        """Labels for this type, falling back to the company-wide list."""
        if self.identifier_labels:
            return self.identifier_labels
        if self.company_id and self.company.asset_identifier_labels:
            return self.company.asset_identifier_labels
        return []

    def build_data_schema_from_fields(self):
        """
        Build a JSON schema compatible with this type's AssetTypeField
        definitions. This keeps schema as the machine-readable contract while
        admins edit field rows.
        """
        properties = {}
        required = []
        # Reading current rows from DB to build the schema.
        field_specs = AssetTypeField.objects.filter(asset_type_id=self.pk).order_by(
            "order", "id"
        )
        for spec in field_specs:
            prop = {"title": spec.label, **json_schema_fragment(spec.type, options=spec.options)}
            properties[spec.name] = prop
            if spec.required:
                required.append(spec.name)

        schema = {
            "type": "object",
            "properties": properties,
            "additionalProperties": True,
        }
        if required:
            schema["required"] = required
        return schema

    def sync_schema_from_fields(self):
        """Persist schema generated from current AssetTypeField rows."""
        schema = self.build_data_schema_from_fields()
        type(self).objects.filter(pk=self.pk).update(schema=schema)
        self.schema = schema


class AssetFieldType(models.TextChoices):
    NUMBER = "number", _("Number")
    TEXT = "text", _("Text")
    BOOLEAN = "boolean", _("Boolean")
    SELECT = "select", _("Select")
    DATE = "date", _("Date")


class AssetTypeField(models.Model):
    """
    Field definition for an asset type. Values captured through the dynamic
    asset form are stored in Asset.data keyed by `name`.
    """
    asset_type = models.ForeignKey(
        AssetType,
        on_delete=models.CASCADE,
        related_name="fields",
        verbose_name=_("Asset Type"),
    )
    name = models.SlugField(
        max_length=100,
        verbose_name=_("Key"),
        help_text=_("Key stored in the asset data (e.g. marca, modelo). Lowercase, no spaces."),
    )
    label = models.CharField(max_length=100, verbose_name=_("Label"))
    type = models.CharField(
        max_length=20,
        choices=AssetFieldType.choices,
        default=AssetFieldType.TEXT,
        verbose_name=_("Type"),
    )
    options = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Options"),
        help_text=_('Choices for "Select" fields, e.g. ["Rojo", "Azul"]'),
    )
    required = models.BooleanField(default=False, verbose_name=_("Required"))
    order = models.PositiveIntegerField(default=0, db_index=True, verbose_name=_("Order"))

    class Meta:
        verbose_name = _("Asset type field")
        verbose_name_plural = _("Asset type fields")
        ordering = ["order", "id"]
        unique_together = ("asset_type", "name")

    def save(self, *args, **kwargs):
        # Same key normalization as Asset.save() so schema keys match data keys.
        self.name = self.name.strip().lower().replace(" ", "_").replace("-", "_")
        super().save(*args, **kwargs)
        if self.asset_type_id:
            self.asset_type.sync_schema_from_fields()

    def delete(self, *args, **kwargs):
        asset_type = self.asset_type if self.asset_type_id else None
        super().delete(*args, **kwargs)
        if asset_type and asset_type.pk:
            asset_type.sync_schema_from_fields()

    def __str__(self):
        return f"{self.label} ({self.asset_type.name})"


class Asset(CompanyOwnedModel):
    asset_type = models.ForeignKey(AssetType, on_delete=models.PROTECT, related_name="assets", verbose_name=_("Asset Type"))
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets", verbose_name=_("Owner/Client"))
    name = models.CharField(max_length=200, verbose_name=_("Name/Label"))
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Flexible Data"))
    current_working_zone = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="current_assets", verbose_name=_("Current Working Zone"))
    zone_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Zone changed at"),
        help_text=_("When current_working_zone last actually changed."),
    )
    provider = models.ForeignKey(Provider, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets", verbose_name=_("Provider"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    assigned_to = models.ForeignKey('company.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_assets", verbose_name=_("Assigned To"))
    history = HistoricalRecords()

    def save(self, *args, **kwargs):
        if self.data:
            self.data = {k.strip().lower().replace(" ", "_"): v for k, v in self.data.items()}
        if self.pk:
            old_zone_id = (
                Asset.objects.filter(pk=self.pk)
                .values_list("current_working_zone_id", flat=True)
                .first()
            )
            if old_zone_id != self.current_working_zone_id:
                self.zone_changed_at = timezone.now()
        elif self.current_working_zone_id:
            self.zone_changed_at = timezone.now()
        super().save(*args, **kwargs)

    @property
    def has_pending_receipt(self) -> bool:
        last_movement = self.transactions.filter(transaction_type=TransactionTypeChoices.MOVEMENT).order_by("-date").first()
        if last_movement is None:
            return False
        last_receipt = self.transactions.filter(transaction_type=TransactionTypeChoices.RECEIPT).order_by("-date").first()
        if last_receipt is None:
            return False
        return last_receipt.date > last_movement.date

    class Meta:
        verbose_name = _("Asset")
        verbose_name_plural = _("Assets")

    def __str__(self):
        return f"{self.name} ({self.asset_type.name})"


class AssetIdentifier(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="identifiers", verbose_name=_("Asset"))
    label = models.CharField(max_length=50, verbose_name=_("Label"))
    value = models.CharField(max_length=100, verbose_name=_("Value"), db_index=True)

    class Meta:
        verbose_name = _("Asset identifier")
        verbose_name_plural = _("Asset identifiers")

    def __str__(self):
        return f"{self.label}: {self.value}"

    def clean(self):
        super().clean()
        if self.asset_id and self.asset.asset_type_id:
            labels = self.asset.asset_type.get_identifier_labels()
            if labels and self.label not in labels:
                raise ValidationError({'label': _("This label is not allowed for this asset type. Allowed: %(labels)s") % {'labels': ", ".join(labels)}})


class AssetMap(CompanyOwnedModel, Timestampable):
    """
    A named, reusable selection of assets to visualize together on a map.
    Marker position is not stored here: it's always resolved live from each
    asset's `current_working_zone`.
    """
    name = models.CharField(max_length=150, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    assets = models.ManyToManyField(
        Asset,
        related_name="maps",
        blank=True,
        verbose_name=_("Assets"),
    )

    class Meta:
        verbose_name = _("Asset map")
        verbose_name_plural = _("Asset maps")
        ordering = ["name"]

    def __str__(self):
        return self.name


class TransactionTypeChoices(models.TextChoices):
    DELIVERY = "delivery", _("Delivery")
    RETURN = "return", _("Return")
    MOVEMENT = "movement", _("Movement")
    RECEIPT = "receipt", _("Receipt")
    PORTE_MOVEMENT = "porte_movement", _("Movement Porte")
    PORTE_DELIVERY = "porte_delivery", _("Delivery Porte")
    MACHINE_HOURS = "machine_hours", _("Machine Hours")
    LABORER_HOURS = "laborer_hours", _("Laborer Hours")


class BillingStatus(models.TextChoices):
    NONE = "none", _("—")
    PENDING = "pending", _("Pending")
    PROCESSED = "processed", _("Processed")
    FAILED = "failed", _("Failed")


class AssetTransaction(CompanyOwnedModel, Timestampable):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, null=True, blank=True, related_name="transactions", verbose_name=_("Asset"))
    from_zone = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions_from", verbose_name=_("From Zone"))
    to_zone = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions_to", verbose_name=_("To Zone"))
    transaction_type = models.CharField(max_length=20, choices=TransactionTypeChoices.choices, verbose_name=_("Transaction Type"))
    date = models.DateTimeField(default=timezone.now, verbose_name=_("Date"))
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Flexible Data"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    firma = models.TextField(blank=True, null=True, verbose_name=_("Signature"))
    firma_image = models.ImageField(upload_to="asset_transactions/firmas/", blank=True, null=True, verbose_name=_("Signature Image"))
    imagen = models.ImageField(upload_to="asset_transactions/images/", blank=True, null=True, verbose_name=_("Image"))
    project_file = models.ForeignKey("management.ProjectFile", on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_transactions", verbose_name=_("Project File"))
    performed_by = models.ForeignKey("company.Employee", on_delete=models.SET_NULL, null=True, blank=True, related_name="performed_transactions")
    billing_status = models.CharField(max_length=10, choices=BillingStatus.choices, default=BillingStatus.NONE, db_index=True, verbose_name=_("Billing Status"))

    class Meta:
        verbose_name = _("Asset transaction")
        verbose_name_plural = _("Asset transactions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                models.expressions.RawSQL("(data->>'machine_start_time')", []),
                name="asset_tx_machine_start_idx",
            ),
            models.Index(
                models.expressions.RawSQL("(data->>'machine_end_time')", []),
                name="asset_tx_machine_end_idx",
            ),
            models.Index(
                models.expressions.RawSQL("(data->>'laborer_start_time')", []),
                name="asset_tx_laborer_start_idx",
            ),
            models.Index(
                models.expressions.RawSQL("(data->>'laborer_end_time')", []),
                name="asset_tx_laborer_end_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.asset} ({self.from_zone} -> {self.to_zone})"


class AssetTransactionPhoto(models.Model):
    class PhotoType(models.TextChoices):
        START = "start", _("Start")
        END = "end", _("End")
        PAUSE = "pause", _("Pause")
        RESUME = "resume", _("Resume")
        OTHER = "other", _("Other")

    transaction = models.ForeignKey(AssetTransaction, on_delete=models.CASCADE, related_name="photos", verbose_name=_("Transaction"))
    photo = models.ImageField(upload_to="asset_transactions/photos/", verbose_name=_("Photo"))
    photo_type = models.CharField(max_length=20, choices=PhotoType.choices, verbose_name=_("Photo Type"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Transaction Photo")
        verbose_name_plural = _("Transaction Photos")
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_photo_type_display()} - {self.transaction}"


class MachinePermission(CompanyOwnedModel):
    employee = models.ForeignKey("company.Employee", on_delete=models.CASCADE, related_name="machine_permissions", verbose_name=_("Employee"))
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="machine_permissions", verbose_name=_("Machine"))
    granted_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Granted At"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))

    class Meta:
        verbose_name = _("Machine Permission")
        verbose_name_plural = _("Machine Permissions")
        constraints = [models.UniqueConstraint(fields=["employee", "asset"], name="unique_machine_permission")]

    def __str__(self):
        return f"{self.employee} → {self.asset}"


class AssetTransactionReceipt(models.Model):
    transaction = models.ForeignKey(AssetTransaction, on_delete=models.CASCADE, related_name="receipts", verbose_name=_("Transaction"))
    foto = models.ImageField(upload_to="transaction_receipts/", blank=True, null=True, verbose_name=_("Foto del Albaran"))
    nro_albaran = models.CharField(max_length=100, verbose_name=_("Nro de Albaran"))
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Flexible Data"))
    movement_transaction = models.ForeignKey(AssetTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="receipt_links", verbose_name=_("Original Movement"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Asset Transaction Receipt")
        verbose_name_plural = _("Asset Transaction Receipts")
        ordering = ["-created_at"]

    def __str__(self):
        return f"Albaran {self.nro_albaran} - {self.transaction.asset}"


class AssetPlanning(CompanyOwnedModel, Timestampable):
    STATUS_CHOICES = [('pending', _('Pending')), ('in_transit', _('In Transit')), ('delivered', _('Delivered')), ('cancelled', _('Cancelled'))]
    PLANNING_CHOICES = [('cuba', _('Container Movement')), ('porte', _('Transport')), ('machine_hours', _('Machine Hours'))]

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="plannings", verbose_name=_("Asset"))
    origin = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="plannings_origin", verbose_name=_("From Zone"))
    destination = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="plannings_destination", verbose_name=_("To Zone"))
    assigned_to = models.ForeignKey('company.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_plannings", verbose_name=_("Assigned To"))
    transport_asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True, related_name="transport_plannings", verbose_name=_("Transport Asset"))
    scheduled_date = models.DateField(verbose_name=_("Scheduled Date"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name=_("Status"))
    route_order = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_("Route Order"))
    planning_type = models.CharField(max_length=20, choices=PLANNING_CHOICES, default='cuba', verbose_name=_("Planning Type"))
    load_description = models.TextField(blank=True, verbose_name=_("Load Description"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset Planning")
        verbose_name_plural = _("Asset Plannings")
        ordering = ["-scheduled_date"]
        indexes = [models.Index(fields=["assigned_to", "scheduled_date"])]

    def __str__(self):
        return f"{self.asset.name} → {self.destination} ({self.scheduled_date})"


class ZoneDistanceCache(models.Model):
    from_zone = models.ForeignKey(WorkingZone, on_delete=models.CASCADE, related_name="+")
    to_zone = models.ForeignKey(WorkingZone, on_delete=models.CASCADE, related_name="+")
    distance_meters = models.PositiveIntegerField()
    duration_seconds = models.PositiveIntegerField()
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Zone Distance Cache")
        verbose_name_plural = _("Zone Distance Cache")
        constraints = [models.UniqueConstraint(fields=["from_zone", "to_zone"], name="unique_zone_pair")]

    def __str__(self):
        return f"{self.from_zone} → {self.to_zone}: {self.distance_meters}m"


class AssetPlanningStop(models.Model):
    planning = models.ForeignKey(AssetPlanning, on_delete=models.CASCADE, related_name="stops")
    zone = models.ForeignKey(WorkingZone, on_delete=models.PROTECT)
    sequence = models.PositiveSmallIntegerField()
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name = _("Planning Stop")
        verbose_name_plural = _("Planning Stops")
        ordering = ["sequence"]
        constraints = [models.UniqueConstraint(fields=["planning", "sequence"], name="unique_stop_sequence")]

    def __str__(self):
        return f"Stop {self.sequence}: {self.zone}"


class AssetDocumentType(BaseDocumentType):
    class Meta:
        verbose_name = _("Asset Document Type")
        verbose_name_plural = _("Asset Document Types")


class AssetDocument(BaseDocument):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="documents")
    document_type = models.ForeignKey(AssetDocumentType, on_delete=models.PROTECT, related_name="documents")

    class Meta:
        verbose_name = _("Asset Document")
        verbose_name_plural = _("Asset Documents")

    def __str__(self):
        return f"{self.document_type.name} - {self.asset}"

    def save(self, *args, **kwargs):
        if not self.company_id and self.asset:
            self.company = self.asset.company
        super().save(*args, **kwargs)