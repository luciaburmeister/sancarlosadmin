from datetime import date as date_, datetime, time, timedelta

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
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
from configurator.utils.evaluator import validate_expression_for_safe_eval
from assets.constants import (
    SERVICE_TYPE_MAQUINA_PROPIA,
    SERVICE_TYPE_MATERIAL_EXTERNO,
    SERVICE_TYPE_MATERIAL_PROPIO,
    SERVICE_TYPE_GRUA,
    SERVICE_TYPE_CUBA,
    SERVICE_TYPE_MAQUINISTAS,
    SERVICE_TYPE_PLANNING_TYPE,
    SERVICE_TYPE_FIELDS,
)
User = get_user_model()


class AssetTypeCategory(models.TextChoices):
    MACHINE = "machine", _("Machine")
    TRUCK = "truck", _("Truck")


class AssetType(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    category = models.CharField(
        max_length=20, choices=AssetTypeCategory.choices, blank=True, verbose_name=_("Category"),
        help_text=_("Used by Asset Planning Requests to offer this type's assets as machines/trucks."),
    )
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


class AssetCurrentStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE", _("Available")
    PICKED_UP = "PICKED_UP", _("Picked Up")
    DELIVERED = "DELIVERED", _("Delivered")


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
    excluded_assets = models.ManyToManyField(
        "self",
        blank=True,
        verbose_name=_("Excluded Assets"),
        help_text=_(
            "Assets this one can never be paired with (e.g. a specific truck "
            "that can't carry a specific cuba), regardless of what the "
            "compatibility rules say. Manual override for one-off cases — "
            "mutual: excluding an asset here also excludes this one from it."
        ),
    )
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

    def current_transaction_status(self, last_transaction=None) -> str:
        """AssetCurrentStatus derived from this asset's own transaction history."""
        if last_transaction is None:
            last_transaction = self.transactions.order_by("-date").first()
        if last_transaction is None:
            return AssetCurrentStatus.AVAILABLE
        if last_transaction.transaction_type == TransactionTypeChoices.MOVEMENT:
            return AssetCurrentStatus.PICKED_UP
        if last_transaction.transaction_type == TransactionTypeChoices.DELIVERY:
            return AssetCurrentStatus.DELIVERED
        if last_transaction.transaction_type == TransactionTypeChoices.RECEIPT:
            prev = self.transactions.filter(
                date__lt=last_transaction.date,
            ).exclude(transaction_type=TransactionTypeChoices.RECEIPT).order_by("-date").first()
            return self.current_transaction_status(prev)
        return AssetCurrentStatus.AVAILABLE

    @property
    def verteo_pending_transaction(self):
        """The last obra pickup awaiting its verteo (RECEIPT), or None if there isn't one."""
        last_movement = self.transactions.filter(
            transaction_type=TransactionTypeChoices.MOVEMENT, from_zone__zone_type=WorkingZone.ZoneType.OBRA,
        ).order_by("-date").first()
        if last_movement is None:
            return None
        if last_movement.data.get("vasAVerter", "") != "Sí":
            return None
        already_done = self.transactions.filter(
            transaction_type=TransactionTypeChoices.RECEIPT, date__gt=last_movement.date,
        ).exists()
        if already_done:
            return None
        return last_movement

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


class TransportCompatibilityRule(CompanyOwnedModel):
    """
    A named condition a candidate truck must pass to be allowed to move a
    given cuba. ALL active rules for the company must evaluate True for a
    truck to be considered compatible — see assets.services.compatibility.
    """
    name = models.CharField(max_length=150, verbose_name=_("Name"))
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    condition_expression = models.TextField(
        verbose_name=_("Condition"),
        help_text=_(
            "Expression evaluated with safe_eval, must return True/False. "
            "Available variables: cuba.<field>, truck.<field> (each asset's "
            "data, plus cuba.size / cuba.operation_type from the planning). "
            "Example: truck.carga_maxima_kg >= cuba.peso_kg and truck.gancho == cuba.tipo_gancho"
        ),
    )
    order = models.PositiveIntegerField(default=0, db_index=True, verbose_name=_("Order"))

    class Meta:
        verbose_name = _("Transport compatibility rule")
        verbose_name_plural = _("Transport compatibility rules")
        ordering = ["order", "id"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        expr = (self.condition_expression or "").strip()
        if not expr:
            raise ValidationError({"condition_expression": _("Condition cannot be empty.")})
        try:
            validate_expression_for_safe_eval(expr)
        except ValueError as e:
            raise ValidationError({"condition_expression": _("Invalid expression: %(error)s") % {"error": e}})


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
    planning = models.ForeignKey("assets.AssetPlanning", on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", verbose_name=_("Planning"))

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

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new:
            self._sync_zones_on_porte_delivery()

    def _sync_zones_on_porte_delivery(self):
        """An own-machine porte delivery means the machine — and, if the plan
        says the operator(s) moved with it, those employees too — are now at
        to_zone. Keeps Asset/Employee.current_working_zone in sync with what
        actually happened in the field.
        """
        if self.transaction_type != TransactionTypeChoices.PORTE_DELIVERY:
            return
        if not self.planning_id or not self.to_zone_id:
            return
        planning = self.planning
        if planning.service_type != SERVICE_TYPE_MAQUINA_PROPIA:
            return
        if self.asset_id:
            self.asset.current_working_zone = self.to_zone
            self.asset.save()
        if planning.machinist_moved:
            for employee in planning.moved_employees.all():
                employee.current_working_zone = self.to_zone
                employee.save()

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


class ServiceType(models.TextChoices):
    """Maps to a backend planning_type ("cuba"/"porte"/"none") via SERVICE_TYPE_PLANNING_TYPE."""
    PORTE_MAQUINA_PROPIA = SERVICE_TYPE_MAQUINA_PROPIA, _("Own Machine Transport")
    PORTE_MATERIAL_EXTERNO = SERVICE_TYPE_MATERIAL_EXTERNO, _("External Material Transport")
    PORTE_MATERIAL_PROPIO = SERVICE_TYPE_MATERIAL_PROPIO, _("Own Material Transport")
    SERVICIO_GRUA = SERVICE_TYPE_GRUA, _("Crane Service")
    MOVIMIENTO_CUBA = SERVICE_TYPE_CUBA, _("Container Movement")
    MOVIMIENTO_MAQUINISTAS = SERVICE_TYPE_MAQUINISTAS, _("Machinist Movement")


class AssetPlanningNotificationRecipient(CompanyOwnedModel, Timestampable):
    """Per-company, per-service-type recipient for 'new AssetPlanningRequest' emails."""
    service_type = models.CharField(max_length=25, choices=ServiceType.choices, verbose_name=_("Service Type"))
    email = models.EmailField(verbose_name=_("Email"))

    class Meta:
        verbose_name = _("Asset Planning Notification Recipient")
        verbose_name_plural = _("Asset Planning Notification Recipients")
        unique_together = ("company", "service_type", "email")
        ordering = ["service_type", "email"]

    def __str__(self):
        return f"{self.get_service_type_display()} → {self.email}"


class CubaOperationType(models.TextChoices):
    PUESTA = "puesta", _("Container Drop-off")
    RETIRADA = "retirada", _("Container Pickup")
    CAMBIO = "cambio", _("Container Swap")
    CAMBIO_AGUA = "cambio_agua", _("Engine Water Tank Refill")


class CubaSize(models.TextChoices):
    M3_3 = "3m3", _("3 M3")
    M3_7 = "7m3", _("7 M3")
    M3_10 = "10m3", _("10 M3")
    M3_12 = "12m3", _("12 M3")
    M3_20 = "20m3", _("20 M3")
    M3_30 = "30m3", _("30 M3")
    AGUA = "agua", _("Water")


class PlanningUrgency(models.TextChoices):
    LOW = "low", _("Low")
    MEDIUM = "medium", _("Medium")
    HIGH = "high", _("High")
    URGENT = "urgent", _("Urgent")


class AssignmentOrigin(models.TextChoices):
    """How assigned_to/transport_asset ended up set — driven by AssetPlanning.save()."""
    UNASSIGNED = "unassigned", _("Unassigned")
    MANUAL = "manual", _("Manual")
    AUTO = "auto", _("Automatic")


# Block fill = service type. Urgency only tints the urgency chip. Used by
# AssetPlanning.service_type_color/urgency_color and the schedule legend.
SERVICE_TYPE_COLORS = {
    SERVICE_TYPE_MAQUINA_PROPIA: "#3B82F6",
    SERVICE_TYPE_MATERIAL_EXTERNO: "#10B981",
    SERVICE_TYPE_MATERIAL_PROPIO: "#14B8A6",
    SERVICE_TYPE_GRUA: "#8B5CF6",
    SERVICE_TYPE_CUBA: "#F59E0B",
    SERVICE_TYPE_MAQUINISTAS: "#EC4899",
}
DEFAULT_SERVICE_TYPE_COLOR = "#64748B"
URGENCY_COLORS = {
    PlanningUrgency.LOW: "#64748B",
    PlanningUrgency.MEDIUM: "#3B82F6",
    PlanningUrgency.HIGH: "#F59E0B",
    PlanningUrgency.URGENT: "#EF4444",
}
def _default_end_time(start: time) -> time | None:
    """1-hour fallback duration when no end time is set; None if that would cross into the next day."""
    combined = datetime.combine(date_.today(), start) + timedelta(hours=1)
    return combined.time() if combined.date() == date_.today() else None


class AbandonedStageChoices(models.TextChoices):
    NOT_STARTED = "not_started", _("Not picked up")
    PICKED_UP_PENDING_POUR = "picked_up_pending_pour", _("Picked up, pending pour")
    PICKED_UP_PENDING_DELIVERY = "picked_up_pending_delivery", _("Picked up, pending delivery")
    DELIVERED_PENDING_CONFIRMATION = "delivered_pending_confirmation", _("Delivered, pending confirmation")


class PlanningStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    IN_TRANSIT = "in_transit", _("In Transit")
    DELIVERED = "delivered", _("Delivered")
    CANCELLED = "cancelled", _("Cancelled")
    NOT_COMPLETED = "not_completed", _("Not Completed")


class PlanningType(models.TextChoices):
    CUBA = "cuba", _("Container Movement")
    PORTE = "porte", _("Transport")
    MACHINE_HOURS = "machine_hours", _("Machine Hours")
    NONE = "none", _("Not Planned")


# Planning types that appear in the driver's daily task queue (GET /plannings/next/, abandon).
# Add new field-task types here when supporting additional workflows or companies.
DRIVER_TASK_PLANNING_TYPES = [PlanningType.CUBA, PlanningType.PORTE]


STATUS_COLORS = {
    PlanningStatus.PENDING: "#64748B",
    PlanningStatus.IN_TRANSIT: "#3B82F6",
    PlanningStatus.DELIVERED: "#10B981",
    PlanningStatus.CANCELLED: "#94A3B8",
    PlanningStatus.NOT_COMPLETED: "#EF4444",
}


class AssetPlanning(CompanyOwnedModel, Timestampable):
    STATUS_CHOICES = PlanningStatus.choices
    PLANNING_CHOICES = PlanningType.choices

    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, null=True, blank=True,
        related_name="plannings", verbose_name=_("Asset"),
    )
    origin = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="plannings_origin", verbose_name=_("From Zone"))
    destination = models.ForeignKey(WorkingZone, on_delete=models.SET_NULL, null=True, blank=True, related_name="plannings_destination", verbose_name=_("To Zone"))
    assigned_to = models.ForeignKey('company.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_plannings", verbose_name=_("Assigned To"))
    transport_asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True, related_name="transport_plannings", verbose_name=_("Transport Asset"))
    suggested_trucks = models.ManyToManyField(
        Asset, blank=True, related_name="planning_truck_suggestions", verbose_name=_("Suggested Trucks"),
        limit_choices_to={"asset_type__category": AssetTypeCategory.TRUCK},
        help_text=_("Optional — narrows which trucks auto-assignment may pick from; leave empty to allow any active truck."),
    )
    scheduled_date = models.DateField(verbose_name=_("Scheduled Date"))
    scheduled_time = models.TimeField(null=True, blank=True, verbose_name=_("Scheduled Time"))
    estimated_duration = models.DurationField(
        null=True, blank=True, verbose_name=_("Estimated Duration"),
        help_text=_("Expected job length. Added to Scheduled Time to compute the end time."),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PlanningStatus.PENDING, verbose_name=_("Status"))
    route_order = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_("Route Order"))
    planning_type = models.CharField(max_length=20, choices=PLANNING_CHOICES, default=PlanningType.CUBA, verbose_name=_("Planning Type"))
    urgency = models.CharField(
        max_length=10, choices=PlanningUrgency.choices, default=PlanningUrgency.MEDIUM, verbose_name=_("Urgency"),
    )
    assignment_origin = models.CharField(
        max_length=12, choices=AssignmentOrigin.choices, default=AssignmentOrigin.UNASSIGNED,
        verbose_name=_("Assignment Origin"),
        help_text=_("Traceability: whether the driver/truck were picked manually or by auto-assignment."),
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.PROTECT,
        null=True,
        blank=False,
        related_name="asset_plannings",
        verbose_name=_("Project File"),
    )
    service_type = models.CharField(max_length=25, choices=ServiceType.choices, blank=True, verbose_name=_("Service Type"))
    cuba_operation_type = models.CharField(max_length=20, choices=CubaOperationType.choices, blank=True, verbose_name=_("Cuba Operation Type"))
    cuba_size = models.CharField(max_length=10, choices=CubaSize.choices, blank=True, verbose_name=_("Cuba Size"))
    machinist_moved = models.BooleanField(
        default=False, verbose_name=_("Machinist Moved"),
        help_text=_("Did the machine operator(s) relocate along with it? Only relevant for Own Machine Transport."),
    )
    moved_employees = models.ManyToManyField(
        'company.Employee', blank=True, related_name="machinist_moved_plannings",
        verbose_name=_("Moved Employees"),
    )
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
        verbose_name=_("Superseded By"),
        help_text=_("Set when a newer planning was created from a re-approved request — this plan is the old one to replace."),
    )
    load_description = models.TextField(blank=True, verbose_name=_("Load Description"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    abandoned_stage = models.CharField(
        max_length=35, blank=True, choices=AbandonedStageChoices.choices, verbose_name=_("Abandoned Stage"),
    )
    abandoned_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Abandoned At"))
    skip_auto_assignment = models.BooleanField(
        default=False, verbose_name=_("Skip Automatic Assignment"),
        help_text=_(
            "When enabled, automatic assignment (bulk or individual) never touches "
            "this planning's truck, driver, or time — full manual override."
        ),
    )
    time_locked = models.BooleanField(
        default=False, verbose_name=_("Time Locked"),
        help_text=_(
            "When enabled, automatic assignment must fit the existing Scheduled "
            "Time/End Time exactly — it won't shift this planning to a different "
            "time the same day if the current slot doesn't work. Ignored while "
            "either time is blank."
        ),
    )
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset Planning")
        verbose_name_plural = _("Asset Plannings")
        ordering = ["-scheduled_date"]
        indexes = [models.Index(fields=["assigned_to", "scheduled_date"])]

    def _is_porte(self) -> bool:
        return (
            self.planning_type == "porte"
            or SERVICE_TYPE_PLANNING_TYPE.get(self.service_type) == "porte"
        )

    def _sync_porte_asset_from_transport(self) -> bool:
        """Fallback: porte plannings without a specifically chosen `asset`
        use the truck as `asset` so the mobile QR/submit flow and admin
        lists always have something to display. A specifically chosen
        `asset` (e.g. the machine for SERVICE_TYPE_MAQUINA_PROPIA) is never
        overwritten. Returns True when `asset` was changed.
        """
        if not self._is_porte() or not self.transport_asset_id:
            return False
        if self.asset_id:
            return False
        self.asset_id = self.transport_asset_id
        return True

    def save(self, *args, **kwargs):
        # Auto-tag assignment_origin unless the caller explicitly set it
        # (TransportAssigner does, to mark AUTO) — see assets/services/transport_assigner.py.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = list(update_fields)
            kwargs["update_fields"] = update_fields
        if self._sync_porte_asset_from_transport() and update_fields is not None and "asset" not in update_fields:
            update_fields = [*update_fields, "asset"]
            kwargs["update_fields"] = update_fields
        origin_explicit = update_fields is not None and "assignment_origin" in update_fields
        if not origin_explicit:
            new_origin = None
            if not self.assigned_to_id or not self.transport_asset_id:
                if self.assignment_origin != AssignmentOrigin.UNASSIGNED:
                    new_origin = AssignmentOrigin.UNASSIGNED
            else:
                previous = (
                    type(self).objects.filter(pk=self.pk).values("assigned_to_id", "transport_asset_id").first()
                    if self.pk else None
                )
                changed = previous is None or (
                    previous["assigned_to_id"] != self.assigned_to_id
                    or previous["transport_asset_id"] != self.transport_asset_id
                )
                if changed:
                    new_origin = AssignmentOrigin.MANUAL
            if new_origin is not None:
                self.assignment_origin = new_origin
                if update_fields is not None:
                    kwargs["update_fields"] = [*update_fields, "assignment_origin"]
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self._sync_porte_asset_from_transport()
        if not self.project_file_id:
            raise ValidationError({"project_file": _("Project File is required.")})
        if self.project_file_id and self.company_id and self.project_file.company_id != self.company_id:
            raise ValidationError({
                "project_file": _("This project file belongs to a different company."),
            })
        required = SERVICE_TYPE_FIELDS.get(self.service_type, set())
        if "asset" in required and not self.asset_id:
            raise ValidationError({"asset": _("Asset is required for this service type.")})

    @property
    def scheduled_end_time(self) -> time | None:
        """
        Computed from scheduled_time + estimated_duration — not stored. Read-only:
        there's no setter, callers set estimated_duration (and scheduled_time)
        directly instead of an end clock-time.
        """
        if not self.scheduled_time or not self.estimated_duration:
            return None
        combined = datetime.combine(date_.today(), self.scheduled_time) + self.estimated_duration
        if combined.date() != date_.today():
            return None  # would cross into the next day — no valid same-day end time
        return combined.time()

    @classmethod
    def has_overlap(cls, *, scheduled_date, start, end, assigned_to_id=None, transport_asset_id=None, exclude_pk=None) -> bool:
        """Does any other still-open (pending/in_transit/not_completed) planning for this driver/truck, same day, overlap [start, end)?

        Shared by has_conflict (checks a saved planning against the rest) and
        TransportAssigner (checks a not-yet-saved candidate slot) — see
        assets/services/transport_assigner.py.
        """
        if not (assigned_to_id or transport_asset_id):
            return False
        q = Q()
        if assigned_to_id:
            q |= Q(assigned_to_id=assigned_to_id)
        if transport_asset_id:
            q |= Q(transport_asset_id=transport_asset_id)
        others = cls.objects.filter(
            q, scheduled_date=scheduled_date,
            status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT, PlanningStatus.NOT_COMPLETED],
        )
        if exclude_pk is not None:
            others = others.exclude(pk=exclude_pk)

        for other in others:
            if not other.scheduled_time:
                continue
            other_end = other.scheduled_end_time or _default_end_time(other.scheduled_time)
            if other_end is None:
                continue
            if start < other_end and other.scheduled_time < end:
                return True
        return False

    @property
    def has_conflict(self) -> bool:
        """Does this (already scheduled) planning overlap another one for the same driver/truck, same day?"""
        if not self.scheduled_time or not (self.assigned_to_id or self.transport_asset_id):
            return False
        end = self.scheduled_end_time or _default_end_time(self.scheduled_time)
        if end is None:
            return False
        return type(self).has_overlap(
            scheduled_date=self.scheduled_date,
            start=self.scheduled_time,
            end=end,
            assigned_to_id=self.assigned_to_id,
            transport_asset_id=self.transport_asset_id,
            exclude_pk=self.pk,
        )

    @property
    def traceability_label(self):
        if not self.assigned_to_id or not self.transport_asset_id or not self.scheduled_time:
            return _("Pending")
        if self.has_conflict:
            return _("Conflict")
        if self.assignment_origin == AssignmentOrigin.AUTO:
            return _("Auto-assigned")
        return _("Manual")

    @property
    def service_type_color(self) -> str:
        return SERVICE_TYPE_COLORS.get(self.service_type, DEFAULT_SERVICE_TYPE_COLOR)

    @property
    def urgency_color(self) -> str:
        return URGENCY_COLORS.get(self.urgency, URGENCY_COLORS[PlanningUrgency.MEDIUM])

    @property
    def status_color(self) -> str:
        return STATUS_COLORS.get(self.status, STATUS_COLORS[PlanningStatus.PENDING])

    @property
    def has_pending_request_changes(self) -> bool:
        """True when the source request was re-opened after approval (awaits re-review)."""
        try:
            req = self.source_request
        except AssetPlanningRequest.DoesNotExist:
            return False
        return req.awaits_rereview

    @property
    def is_superseded(self) -> bool:
        return self.superseded_by_id is not None

    @property
    def is_old_plan(self) -> bool:
        """Plan to replace: linked request awaits re-review, or already replaced by a newer approve."""
        return self.has_pending_request_changes or self.is_superseded

    def compute_abandoned_stage(self) -> str:
        """Where a driver left this job — used when abandoning it via "No puedo hacer más"."""
        if self.planning_type == PlanningType.PORTE:
            return (
                AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY if self.status == PlanningStatus.IN_TRANSIT
                else AbandonedStageChoices.NOT_STARTED
            )
        if self.asset_id is None:
            return AbandonedStageChoices.NOT_STARTED
        last_tx = self.transactions.order_by("-date").first() or self.asset.transactions.order_by("-date").first()
        asset_state = self.asset.current_transaction_status(last_tx)
        if asset_state == AssetCurrentStatus.AVAILABLE:
            return AbandonedStageChoices.NOT_STARTED
        if asset_state == AssetCurrentStatus.DELIVERED:
            return AbandonedStageChoices.DELIVERED_PENDING_CONFIRMATION
        if self.asset.verteo_pending_transaction is not None:
            return AbandonedStageChoices.PICKED_UP_PENDING_POUR
        return AbandonedStageChoices.PICKED_UP_PENDING_DELIVERY

    def __str__(self):
        asset_label = self.asset.name if self.asset_id else _("Planning")
        return f"{asset_label} → {self.destination} ({self.scheduled_date})"


class AssetPlanningRequestStatus(models.TextChoices):
    PENDING_REVIEW = "pending_review", _("Pending Review")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")


class AssetPlanningRequest(CompanyOwnedModel, Timestampable):
    """Request for an AssetPlanning, reviewed and approved before it becomes one."""
    service_type = models.CharField(max_length=25, choices=ServiceType.choices, default=SERVICE_TYPE_MAQUINA_PROPIA, verbose_name=_("Service Type"))
    # Derived from service_type in clean().
    planning_type = models.CharField(max_length=20, choices=AssetPlanning.PLANNING_CHOICES, default="porte", verbose_name=_("Planning Type"))
    to_zone = models.ForeignKey(WorkingZone, on_delete=models.PROTECT, related_name="planning_requests_to", verbose_name=_("To Zone"))
    from_zone = models.ForeignKey(WorkingZone, on_delete=models.PROTECT, null=True, blank=True, related_name="planning_requests_from", verbose_name=_("From Zone"))
    needed_date = models.DateField(default=date_.today, verbose_name=_("Needed Date"))
    needed_time = models.TimeField(
        null=True, blank=True, verbose_name=_("Needed Time"),
        help_text=_("Optional — leave blank to let route assignment pick a time."),
    )
    estimated_duration = models.DurationField(
        null=True, blank=True, verbose_name=_("Estimated Duration"),
        help_text=_("Expected job length. Used with Needed Time to find a free slot."),
    )
    urgency = models.CharField(
        max_length=10, choices=PlanningUrgency.choices, default=PlanningUrgency.MEDIUM, verbose_name=_("Urgency"),
        help_text=_("Drives processing priority when auto-assigning drivers/trucks."),
    )
    cuba_operation_type = models.CharField(max_length=20, choices=CubaOperationType.choices, blank=True, verbose_name=_("Cuba Operation Type"))
    cuba_size = models.CharField(max_length=10, choices=CubaSize.choices, blank=True, verbose_name=_("Cuba Size"))
    machinist_moved = models.BooleanField(
        default=False, verbose_name=_("Machinist Moved"),
        help_text=_("Did the machine operator(s) relocate along with it? Only relevant for Own Machine Transport."),
    )
    moved_employees = models.ManyToManyField(
        'company.Employee', blank=True, related_name="machinist_moved_requests",
        verbose_name=_("Moved Employees"),
    )
    load_description = models.TextField(blank=True, verbose_name=_("Load Description"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    requested_by = models.ForeignKey('company.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_planning_requests", verbose_name=_("Requested By"))

    status = models.CharField(max_length=20, choices=AssetPlanningRequestStatus.choices, default=AssetPlanningRequestStatus.PENDING_REVIEW, verbose_name=_("Status"))

    # Suggested by the requester, confirmed/corrected by the reviewer.
    assigned_to = models.ForeignKey(
        'company.Employee', on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_planning_review_assignments", verbose_name=_("Assigned To"),
        help_text=_("Optional — leave blank to let route assignment pick an available driver."),
    )
    asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True, related_name="planning_requests", verbose_name=_("Asset"))
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.PROTECT,
        null=True,
        blank=False,
        related_name="asset_planning_requests",
        verbose_name=_("Project File"),
    )
    suggested_trucks = models.ManyToManyField(
        Asset, blank=True, related_name="planning_request_truck_suggestions", verbose_name=_("Suggested Trucks"),
        limit_choices_to={"asset_type__category": AssetTypeCategory.TRUCK},
        help_text=_("Optional — narrows which trucks route assignment may pick from; leave empty to allow any active truck."),
    )
    # The single truck confirmed by the reviewer out of suggested_trucks; used to create the resulting AssetPlanning.
    transport_asset = models.ForeignKey(
        Asset, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="planning_request_transport_selections", verbose_name=_("Transport Asset"),
        help_text=_("Optional — leave blank to let route assignment pick an available truck."),
    )

    # Reviewer-only.
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Reviewed By"))
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Reviewed At"))
    rejection_reason = models.TextField(blank=True, verbose_name=_("Rejection Reason"))

    # Set once the request is approved.
    resulting_planning = models.OneToOneField(AssetPlanning, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_request", verbose_name=_("Asset Planning"))

    # Only used by service types with no AssetPlanning at all (e.g. Machinist
    # Movement — see apply_machinist_zone_change()): when the zone change was
    # actually applied to moved_employees, immediately on approval or later
    # by apply_due_machinist_movements() if needed_date was in the future.
    zone_sync_applied_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Zone Sync Applied At"))

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset Planning Request")
        verbose_name_plural = _("Asset Planning Requests")
        ordering = ["-created_at"]
        permissions = [
            ("approve_assetplanningrequest", "Can approve/reject asset planning requests"),
        ]

    def __str__(self):
        return f"{self.get_service_type_display()} → {self.to_zone} ({self.needed_date})"

    @property
    def awaits_rereview(self) -> bool:
        """Pending again after a prior approval — previously created plan still linked."""
        return (
            self.status == AssetPlanningRequestStatus.PENDING_REVIEW
            and self.resulting_planning_id is not None
        )

    # Fields a reviewer acts on; editing any of these after a decision was
    # made invalidates that decision (see save()).
    REVIEW_TRACKED_FIELDS = [
        "service_type", "to_zone_id", "from_zone_id", "needed_date", "needed_time", "estimated_duration",
        "urgency", "cuba_operation_type", "cuba_size", "machinist_moved", "load_description", "notes",
        "assigned_to_id", "asset_id", "transport_asset_id", "project_file_id",
    ]

    def save(self, *args, **kwargs):
        if self.pk and self.status != AssetPlanningRequestStatus.PENDING_REVIEW:
            previous = AssetPlanningRequest.objects.filter(pk=self.pk).values(
                "status", *self.REVIEW_TRACKED_FIELDS
            ).first()
            # Only auto-revert when this save doesn't already carry an explicit
            # status change (i.e. it's a plain edit, not the approve/reject action).
            if previous and previous["status"] == self.status and any(
                getattr(self, field) != previous[field] for field in self.REVIEW_TRACKED_FIELDS
            ):
                self.status = AssetPlanningRequestStatus.PENDING_REVIEW
                self.reviewed_by = None
                self.reviewed_at = None
                self.rejection_reason = ""
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        self.planning_type = SERVICE_TYPE_PLANNING_TYPE.get(self.service_type, self.planning_type)
        required = SERVICE_TYPE_FIELDS.get(self.service_type, set())

        # to_zone is always present, so company is derived from it.
        if self.to_zone_id and not self.company_id:
            self.company = self.to_zone.company

        if "from_zone" in required and not self.from_zone_id:
            raise ValidationError({"from_zone": _("From Zone is required for this service type.")})
        if "from_zone" not in required:
            self.from_zone = None
        elif self.from_zone_id and self.company_id and self.from_zone.company_id != self.company_id:
            raise ValidationError({"from_zone": _("From Zone belongs to a different company than To Zone.")})

        if "asset" in required and not self.asset_id:
            raise ValidationError({"asset": _("Asset is required for this service type.")})
        if "asset" not in required:
            self.asset = None
        elif self.asset_id and self.company_id and self.asset.company_id != self.company_id:
            raise ValidationError({"asset": _("This asset belongs to a different company than the selected zone.")})

        if "load_description" in required and not self.load_description:
            raise ValidationError({"load_description": _("Load description is required for this service type.")})

        if "cuba_operation_type" in required and not self.cuba_operation_type:
            raise ValidationError({"cuba_operation_type": _("Cuba operation type is required for this service type.")})
        if "cuba_operation_type" not in required:
            self.cuba_operation_type = ""

        if "cuba_size" in required and not self.cuba_size:
            raise ValidationError({"cuba_size": _("Cuba size is required for this service type.")})
        if "cuba_size" not in required:
            self.cuba_size = ""

        # Not tracked in SERVICE_TYPE_FIELDS: AssetPlanningRequestDraft shares
        # that dict and has no estimated_duration field at all.
        if self.service_type == SERVICE_TYPE_MAQUINISTAS:
            self.estimated_duration = None
        elif not self.estimated_duration:
            raise ValidationError({"estimated_duration": _("Estimated duration is required for this service type.")})

        if self.service_type != SERVICE_TYPE_MAQUINA_PROPIA:
            self.machinist_moved = False

        # Optional for every service type: a suggestion the route assignment
        # algorithm (or the reviewer) fills in or overrides if left blank.
        if self.assigned_to_id and self.company_id and self.assigned_to.company_id != self.company_id:
            raise ValidationError({"assigned_to": _("This employee belongs to a different company than the selected zone.")})

        if not self.project_file_id:
            raise ValidationError({"project_file": _("Project File is required.")})
        if self.project_file_id and self.company_id and self.project_file.company_id != self.company_id:
            raise ValidationError({"project_file": _("This project file belongs to a different company than the selected zone.")})

    def get_estimated_duration(self):
        from assets.services.route_optimizer import GoogleRouteOptimizer
        return GoogleRouteOptimizer().estimate_duration(self.from_zone, self.to_zone)

    def apply_machinist_zone_change(self):
        """Move every selected employee's current_working_zone to to_zone and mark this applied.

        Only meaningful for MOVIMIENTO_MAQUINISTAS requests, which never get an
        AssetPlanning — approve_submit_action calls this immediately when
        needed_date is due, otherwise apply_due_machinist_movements() (Celery
        beat) calls it once that date arrives.
        """
        for employee in self.moved_employees.all():
            employee.current_working_zone = self.to_zone
            employee.save()
        self.zone_sync_applied_at = timezone.now()
        self.save(update_fields=["zone_sync_applied_at"])


class AssetPlanningRequestDraftStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SUBMITTED = "submitted", _("Submitted")
    PROCESSED = "processed", _("Processed")
    CANCELLED = "cancelled", _("Cancelled")


class AssetPlanningRequestDraft(CompanyOwnedModel, Timestampable):
    """
    Mobile-app draft of a planning request. Client/zones can be picked from the
    real Client/WorkingZone catalogs, or — when the requester can't find one —
    described with the new_*/*_text/*_city suggestion fields instead. Never
    becomes an AssetPlanningRequest automatically — a planner reads it and
    creates the real one by hand (see AssetPlanningRequestAdmin's "Create real
    request" action), at which point this draft is linked via `resulting_request`.
    """
    service_type = models.CharField(max_length=25, choices=ServiceType.choices, default=SERVICE_TYPE_MAQUINA_PROPIA, verbose_name=_("Service Type"))
    client = models.ForeignKey(
        Client, on_delete=models.PROTECT, null=True, blank=True,
        related_name="asset_planning_request_drafts", verbose_name=_("Client"),
        help_text=_("Leave empty and fill New Client Name instead if this client doesn't exist yet."),
    )
    new_client_name = models.CharField(
        max_length=100, blank=True, verbose_name=_("New Client Name (suggested)"),
        help_text=_("Filled instead of Client when the requester couldn't find the client — a new one must be created."),
    )
    new_client_tax_id = models.CharField(max_length=20, blank=True, verbose_name=_("New Client Tax ID (CIF/NIF, suggested)"))
    new_client_fiscal_address = models.CharField(max_length=255, blank=True, verbose_name=_("New Client Fiscal Address (suggested)"))
    to_zone = models.ForeignKey(
        WorkingZone, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("To Zone"),
        help_text=_("Leave empty and fill Destination Street/City instead if this zone doesn't exist yet."),
    )
    destination_text = models.CharField(
        max_length=255, blank=True, verbose_name=_("New Destination Street/Number (suggested)"),
        help_text=_("Filled instead of To Zone when the requester couldn't find the zone — a new one must be created."),
    )
    destination_city = models.CharField(max_length=100, blank=True, default="Málaga", verbose_name=_("New Destination City (suggested)"))
    from_zone = models.ForeignKey(
        WorkingZone, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("From Zone"),
        help_text=_("Leave empty and fill Origin Street/City instead if this zone doesn't exist yet."),
    )
    origin_text = models.CharField(
        max_length=255, blank=True, verbose_name=_("New Origin Street/Number (suggested)"),
        help_text=_("Filled instead of From Zone when the requester couldn't find the zone — a new one must be created."),
    )
    origin_city = models.CharField(max_length=100, blank=True, default="Málaga", verbose_name=_("New Origin City (suggested)"))
    needed_date = models.DateField(verbose_name=_("Needed Date"))
    needed_time = models.TimeField(null=True, blank=True, verbose_name=_("Preferred Time"))
    urgency = models.CharField(max_length=10, choices=PlanningUrgency.choices, blank=True, verbose_name=_("Urgency"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    cuba_operation_type = models.CharField(max_length=20, choices=CubaOperationType.choices, blank=True, verbose_name=_("Cuba Operation Type"))
    cuba_size = models.CharField(max_length=10, choices=CubaSize.choices, blank=True, verbose_name=_("Cuba Size"))
    asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, blank=True, related_name="planning_request_drafts", verbose_name=_("Asset"))
    load_description = models.TextField(blank=True, verbose_name=_("Load Description"))
    requested_by = models.ForeignKey('company.Employee', on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_planning_request_drafts", verbose_name=_("Requested By"))
    status = models.CharField(max_length=20, choices=AssetPlanningRequestDraftStatus.choices, default=AssetPlanningRequestDraftStatus.DRAFT, verbose_name=_("Status"))
    resulting_request = models.OneToOneField(
        AssetPlanningRequest, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_draft", verbose_name=_("Asset Planning Request"),
    )

    class Meta:
        verbose_name = _("Asset Planning Request Draft")
        verbose_name_plural = _("Asset Planning Request Drafts")
        ordering = ["-created_at"]

    def __str__(self):
        client_label = self.new_client_name or (self.client.name if self.client_id else "?")
        destination_label = self.to_zone.name if self.to_zone_id else (self.destination_text or "?")
        return f"{self.get_service_type_display()} → {destination_label} ({client_label}, {self.needed_date})"

    @property
    def requests_new_client(self) -> bool:
        """True when the requester couldn't find their client and suggested a new one instead."""
        return bool(self.new_client_name) and not self.client_id

    @property
    def requests_new_destination_zone(self) -> bool:
        """True when the requester couldn't find the destination zone and described a new one instead."""
        return bool(self.destination_text) and not self.to_zone_id

    @property
    def requests_new_origin_zone(self) -> bool:
        """True when the requester couldn't find the origin zone and described a new one instead."""
        return bool(self.origin_text) and not self.from_zone_id

    @classmethod
    def count_submitted_for(cls, companies) -> int:
        """How many drafts are SUBMITTED and awaiting a planner, across the given companies."""
        return cls.objects.filter(
            status=AssetPlanningRequestDraftStatus.SUBMITTED, company__in=companies,
        ).count()

    def missing_fields_for_submit(self):
        """Field names still needed before this draft can move to SUBMITTED."""
        needed = SERVICE_TYPE_FIELDS.get(self.service_type, set())
        required = {
            field_name for field_name in needed
            if field_name not in ("suggested_trucks", "from_zone", "to_zone")
        }
        required |= {"needed_date"}

        missing = []
        for field_name in required:
            value = getattr(self, f"{field_name}_id" if field_name == "asset" else field_name)
            if not value:
                missing.append(field_name)
        if not self.client_id and not self.new_client_name.strip():
            missing.append("client")
        if not self.to_zone_id and not self.destination_text.strip():
            missing.append("destination_text")
        if "from_zone" in needed and not self.from_zone_id and not self.origin_text.strip():
            missing.append("origin_text")
        return missing


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