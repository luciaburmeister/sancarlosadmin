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
from assets.constants import (
    SERVICE_TYPE_MAQUINA_PROPIA,
    SERVICE_TYPE_MATERIAL_EXTERNO,
    SERVICE_TYPE_GRUA,
    SERVICE_TYPE_CUBA,
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


class ServiceType(models.TextChoices):
    """Maps to a backend planning_type ("cuba"/"porte") via SERVICE_TYPE_PLANNING_TYPE."""
    PORTE_MAQUINA_PROPIA = SERVICE_TYPE_MAQUINA_PROPIA, _("Own Machine Transport")
    PORTE_MATERIAL_EXTERNO = SERVICE_TYPE_MATERIAL_EXTERNO, _("External Material Transport")
    SERVICIO_GRUA = SERVICE_TYPE_GRUA, _("Crane Service")
    MOVIMIENTO_CUBA = SERVICE_TYPE_CUBA, _("Container Movement")


class CubaOperationType(models.TextChoices):
    PUESTA = "puesta", _("Container Drop-off")
    RETIRADA = "retirada", _("Container Pickup")
    CAMBIO = "cambio", _("Container Swap")


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
    SERVICE_TYPE_GRUA: "#8B5CF6",
    SERVICE_TYPE_CUBA: "#F59E0B",
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


class AssetPlanning(CompanyOwnedModel, Timestampable):
    STATUS_CHOICES = [('pending', _('Pending')), ('in_transit', _('In Transit')), ('delivered', _('Delivered')), ('cancelled', _('Cancelled'))]
    PLANNING_CHOICES = [('cuba', _('Container Movement')), ('porte', _('Transport')), ('machine_hours', _('Machine Hours'))]

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
    scheduled_end_time = models.TimeField(
        null=True, blank=True, verbose_name=_("Scheduled End Time"),
        help_text=_("Ideal completion time — set by the requester or computed by auto-assignment."),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name=_("Status"))
    route_order = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_("Route Order"))
    planning_type = models.CharField(max_length=20, choices=PLANNING_CHOICES, default='cuba', verbose_name=_("Planning Type"))
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
    service_type = models.CharField(max_length=20, choices=ServiceType.choices, blank=True, verbose_name=_("Service Type"))
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
    load_description = models.TextField(blank=True, verbose_name=_("Load Description"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset Planning")
        verbose_name_plural = _("Asset Plannings")
        ordering = ["-scheduled_date"]
        indexes = [models.Index(fields=["assigned_to", "scheduled_date"])]

    def save(self, *args, **kwargs):
        # Auto-tag assignment_origin unless the caller explicitly set it
        # (SimpleAssigner does, to mark AUTO) — see assets/services/auto_assigner.py.
        update_fields = kwargs.get("update_fields")
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
        if not self.project_file_id:
            raise ValidationError({"project_file": _("Project File is required.")})
        if self.project_file_id and self.company_id and self.project_file.company_id != self.company_id:
            raise ValidationError({
                "project_file": _("This project file belongs to a different company."),
            })
        required = SERVICE_TYPE_FIELDS.get(self.service_type, set())
        if "asset" in required and not self.asset_id:
            raise ValidationError({"asset": _("Asset is required for this service type.")})

    @classmethod
    def has_overlap(cls, *, scheduled_date, start, end, assigned_to_id=None, transport_asset_id=None, exclude_pk=None) -> bool:
        """Does any other pending/in_transit planning for this driver/truck, same day, overlap [start, end)?

        Shared by has_conflict (checks a saved planning against the rest) and
        SimpleAssigner (checks a not-yet-saved candidate slot) — see
        assets/services/auto_assigner.py.
        """
        if not (assigned_to_id or transport_asset_id):
            return False
        q = Q()
        if assigned_to_id:
            q |= Q(assigned_to_id=assigned_to_id)
        if transport_asset_id:
            q |= Q(transport_asset_id=transport_asset_id)
        others = cls.objects.filter(q, scheduled_date=scheduled_date, status__in=["pending", "in_transit"])
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

    def __str__(self):
        asset_label = self.asset.name if self.asset_id else _("Planning")
        return f"{asset_label} → {self.destination} ({self.scheduled_date})"


class AssetPlanningRequestStatus(models.TextChoices):
    PENDING_REVIEW = "pending_review", _("Pending Review")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")


class AssetPlanningRequest(CompanyOwnedModel, Timestampable):
    """Request for an AssetPlanning, reviewed and approved before it becomes one."""
    service_type = models.CharField(max_length=20, choices=ServiceType.choices, default=SERVICE_TYPE_MAQUINA_PROPIA, verbose_name=_("Service Type"))
    # Derived from service_type in clean().
    planning_type = models.CharField(max_length=20, choices=AssetPlanning.PLANNING_CHOICES, default="porte", verbose_name=_("Planning Type"))
    to_zone = models.ForeignKey(WorkingZone, on_delete=models.PROTECT, related_name="planning_requests_to", verbose_name=_("To Zone"))
    from_zone = models.ForeignKey(WorkingZone, on_delete=models.PROTECT, null=True, blank=True, related_name="planning_requests_from", verbose_name=_("From Zone"))
    needed_date = models.DateField(verbose_name=_("Needed Date"))
    needed_time = models.TimeField(
        null=True, blank=True, verbose_name=_("Needed Time"),
        help_text=_("Optional — leave blank to let route assignment pick a time."),
    )
    end_time = models.TimeField(
        null=True, blank=True, verbose_name=_("End Time"),
        help_text=_(
            "Optional — estimated/ideal completion time. Used to work backward to a "
            "start time when Needed Time is left blank."
        ),
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

    # Fields a reviewer acts on; editing any of these after a decision was
    # made invalidates that decision (see save()).
    REVIEW_TRACKED_FIELDS = [
        "service_type", "to_zone_id", "from_zone_id", "needed_date", "needed_time", "end_time",
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