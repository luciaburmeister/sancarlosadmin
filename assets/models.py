import json
import secrets
from collections import defaultdict
from datetime import date as date_, datetime, time, timedelta

from django.contrib.postgres.fields import ArrayField
from django.db import models, transaction
from django.db.models import Exists, F, OuterRef, Prefetch, Q, Subquery, prefetch_related_objects
from django.utils.translation import gettext_lazy as _, pgettext_lazy
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
    SERVICE_TYPE_AUTOPORTE_MAQUINA,
    SERVICE_TYPE_PEONES,
    SERVICE_TYPE_PLANNING_TYPE,
    SERVICE_TYPE_FIELDS,
    SERVICE_TYPES_NEEDING_MOVED_EMPLOYEES,
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


class AssetOperationalStatus(models.TextChoices):
    ACTIVE   = "active",   _("Active")
    PARTIAL  = "partial",  _("Partial")
    INACTIVE = "inactive", _("Inactive")


class AssetFaultSeverity(models.TextChoices):
    PARTIAL = "partial", _("Partial")
    TOTAL   = "total",   _("Total")


class AssetFaultStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    FIXED   = "fixed",   pgettext_lazy("asset_fault_status", "Fixed")


class CubaTransactionKey:
    ESTADO = "cubaEstado"
    VAS_A_VERTER = "vasAVerter"
    LANDFILL = "vertedero"


class AssetDataKey:
    CAPACIDAD = "capacidad"


class CubaFillState(models.TextChoices):
    FULL = "Cuba llena", _("Full container")
    EMPTY = "Cuba vacía", _("Empty container")


class FormAffirmative(models.TextChoices):
    YES = "Sí", _("Yes")
    NO = "No", _("No")


class PorteAction(models.TextChoices):
    RECOGIDA = "recogida", _("Pickup")
    CARGA = "carga", _("Load")
    ENTREGA = "entrega", _("Delivery")


class PorteTransactionDataKey:
    MOVEMENT_TYPE = "movement_type"
    ACTION = "action"
    MATRICULA = "matricula"
    PLANNING_ID = "planning_id"
    LOAD_DESCRIPTION = "load_description"
    CLIENT_ID = "client_id"
    MACHINE_ID = "machine_id"
    GOODS_TYPE = "goods_type"
    TONNES_TRANSPORTED = "tonnes_transported"
    PACKAGE_COUNT = "package_count"
    # reference_number used to live here too -- now a real AssetTransaction
    # model field (see migration 0078), not a `data` key.


class PorteMovementKind:
    PORTE = "porte"


class ChecklistStepKey:
    RECOGER = "recoger"
    RECOGER_CUBA = "recoger_cuba"
    RECOGER_VIEJA = "recoger_vieja"
    DEJAR = "dejar"
    DEJAR_NUEVA = "dejar_nueva"
    CARGA = "carga"
    ENTREGA = "entrega"
    VERTER = "verter"


class ChecklistStepStatus:
    DONE = "done"
    PENDING = "pending"
    SKIPPED = "skipped"


class AssetQuerySet(models.QuerySet):
    def with_pending_pickup_by(self, employee, include_porte=False):
        """Assets `employee` still has on their truck — their MOVEMENT is the most
        recent transaction once RECEIPT (verteo) rows are ignored, same as
        Asset.current_transaction_status treats RECEIPT as transparent, only
        DELIVERY (or someone else's MOVEMENT) actually resolves it.         Full or empty. See Asset.verteo_pending_transaction for the narrower
        "still owes a verteo" signal.

        include_porte=True also treats PORTE_MOVEMENT as pickup and PORTE_LOAD /
        PORTE_DELIVERY as transparent (like RECEIPT), so a cuba stays on the truck
        through a full porte cycle.
        """
        pickup_types = [TransactionTypeChoices.MOVEMENT]
        transparent_types = [TransactionTypeChoices.RECEIPT]
        if include_porte:
            pickup_types.append(TransactionTypeChoices.PORTE_MOVEMENT)
            transparent_types.extend([
                TransactionTypeChoices.PORTE_LOAD,
                TransactionTypeChoices.PORTE_DELIVERY,
            ])
        latest_relevant_transaction = AssetTransaction.objects.filter(
            asset=OuterRef("pk"),
        ).exclude(
            transaction_type__in=transparent_types,
        ).order_by("-date").values("id")[:1]
        pending_pickups = AssetTransaction.objects.filter(
            transaction_type__in=pickup_types, performed_by=employee,
        )
        return self.filter(company=employee.company).exclude(
            asset_type__category__in=[AssetTypeCategory.MACHINE, AssetTypeCategory.TRUCK],
        ).annotate(
            latest_relevant_transaction_id=Subquery(latest_relevant_transaction),
        ).filter(
            latest_relevant_transaction_id__in=pending_pickups.values("id"),
        ).annotate(
            pending_pickup_date=Subquery(
                pending_pickups.filter(
                    pk=OuterRef("latest_relevant_transaction_id"),
                ).values("date")[:1],
            ),
        ).order_by("-pending_pickup_date")

    def with_verteo_pending_by(self, employee):
        """Assets `employee` picked up full and hasn't verted yet — survives a
        DELIVERY on purpose, unlike with_pending_pickup_by above: a full cuba can
        be dropped off at an almacén and still owe its verteo. Only a RECEIPT
        resolves this one."""
        my_last_movement = AssetTransaction.objects.filter(
            asset=OuterRef("pk"), transaction_type=TransactionTypeChoices.MOVEMENT, performed_by=employee,
        ).order_by("-date")
        return self.filter(company=employee.company).annotate(
            my_last_movement_id=Subquery(my_last_movement.values("id")[:1]),
            my_last_movement_date=Subquery(my_last_movement.values("date")[:1]),
        ).annotate(
            my_last_movement_full=Exists(
                AssetTransaction.objects.filter(pk=OuterRef("my_last_movement_id")).filter(
                    Q(**{f"data__{CubaTransactionKey.ESTADO}": CubaFillState.FULL})
                    | Q(**{f"data__{CubaTransactionKey.VAS_A_VERTER}": FormAffirmative.YES}),
                ),
            ),
        ).filter(
            my_last_movement_full=True,
        ).exclude(
            transactions__transaction_type=TransactionTypeChoices.RECEIPT,
            transactions__date__gt=F("my_last_movement_date"),
        )

    def needing_driver_attention(self, employee, include_porte=False):
        """Verteo-pending first, else still physically on the truck. Verteo
        never applies to porte (it never tracks llena/vacía), so include_porte
        only widens the second check. When several assets qualify, the one whose
        pickup is most recent wins."""
        stuck = self.with_verteo_pending_by(employee).order_by("-my_last_movement_date").first()
        if stuck is not None:
            return stuck
        return self.with_pending_pickup_by(employee, include_porte=include_porte).first()

    def visible_pending_dropoff(self, employee, planning=None):
        """Pending cuba the driver-facing UI would show — same rules as
        /plannings/next/. Assign must use this (not raw needing_driver_attention)
        so a ghost stuck cuba the Home card hides cannot block QR scans."""
        stuck_asset = self.needing_driver_attention(employee, include_porte=True)
        if stuck_asset is None:
            return None
        last_movement = stuck_asset.transactions.filter(
            transaction_type__in=[
                TransactionTypeChoices.MOVEMENT,
                TransactionTypeChoices.PORTE_MOVEMENT,
            ],
        ).order_by("-date").first()
        if last_movement is None or last_movement.planning_id is None:
            return None
        pending = {
            "planning_id": last_movement.planning_id,
            "asset_id": stuck_asset.id,
            "asset_name": stuck_asset.name,
            "verteo_pending": stuck_asset.verteo_pending_transaction is not None,
        }
        if planning is not None and planning.suppresses_pending_dropoff_warning(pending):
            return None
        return pending


class Asset(CompanyOwnedModel):
    objects = AssetQuerySet.as_manager()

    asset_type = models.ForeignKey(AssetType, on_delete=models.PROTECT, related_name="assets", verbose_name=_("Asset Type"))
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets", verbose_name=_("Owner/Client"))
    name = models.CharField(max_length=200, verbose_name=_("Name/Label"))
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    operational_status = models.CharField(
        max_length=20,
        choices=AssetOperationalStatus.choices,
        default=AssetOperationalStatus.ACTIVE,
        editable=False,
        verbose_name=_("Operational Status"),
    )
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

    def prefetched_transactions(self):
        """This asset's prefetched `transactions`, newest first by `date`, or
        None if nothing was prefetched.

        Callers (on this model and in AssetListItemSerializer) use this to
        avoid a fresh query per asset when listing many assets at once —
        the caller must have prefetched with an explicit `order_by("-date")`
        queryset, since AssetTransaction's own default ordering is
        `-created_at`. When None, callers fall back to their original
        per-call queryset, so single-asset lookups (San Carlos app, admin,
        etc.) are unaffected.
        """
        cache = getattr(self, "_prefetched_objects_cache", {})
        prefetched = cache.get("transactions")
        if prefetched is None:
            return None
        return sorted(prefetched, key=lambda t: t.date, reverse=True)

    @property
    def has_pending_receipt(self) -> bool:
        cached = self.prefetched_transactions()
        if cached is not None:
            last_movement = next((t for t in cached if t.transaction_type == TransactionTypeChoices.MOVEMENT), None)
            if last_movement is None:
                return False
            last_receipt = next((t for t in cached if t.transaction_type == TransactionTypeChoices.RECEIPT), None)
        else:
            last_movement = self.transactions.filter(transaction_type=TransactionTypeChoices.MOVEMENT).order_by("-date").first()
            if last_movement is None:
                return False
            last_receipt = self.transactions.filter(transaction_type=TransactionTypeChoices.RECEIPT).order_by("-date").first()
        if last_receipt is None:
            return False
        return last_receipt.date > last_movement.date

    def current_transaction_status(self, last_transaction=None) -> str:
        """AssetCurrentStatus derived from this asset's own transaction history."""
        cached = self.prefetched_transactions()
        if last_transaction is None:
            last_transaction = (cached[0] if cached else None) if cached is not None else self.transactions.order_by("-date").first()
        if last_transaction is None:
            return AssetCurrentStatus.AVAILABLE
        if last_transaction.transaction_type in (
            TransactionTypeChoices.MOVEMENT,
            TransactionTypeChoices.PORTE_MOVEMENT,
        ):
            return AssetCurrentStatus.PICKED_UP
        if last_transaction.transaction_type == TransactionTypeChoices.DELIVERY:
            return AssetCurrentStatus.DELIVERED
        if last_transaction.transaction_type in (
            TransactionTypeChoices.RECEIPT,
            TransactionTypeChoices.PORTE_LOAD,
            TransactionTypeChoices.PORTE_DELIVERY,
        ):
            excluded_types = (
                TransactionTypeChoices.RECEIPT,
                TransactionTypeChoices.PORTE_LOAD,
                TransactionTypeChoices.PORTE_DELIVERY,
            )
            if cached is not None:
                prev = next(
                    (t for t in cached if t.date < last_transaction.date and t.transaction_type not in excluded_types),
                    None,
                )
            else:
                prev = self.transactions.filter(
                    date__lt=last_transaction.date,
                ).exclude(
                    transaction_type__in=excluded_types,
                ).order_by("-date").first()
            return self.current_transaction_status(prev)
        return AssetCurrentStatus.AVAILABLE

    def recalculate_operational_status(self):
        pending = self.faults.filter(status=AssetFaultStatus.PENDING)
        if not pending.exists():
            new_status = AssetOperationalStatus.ACTIVE
        elif pending.filter(severity=AssetFaultSeverity.TOTAL).exists():
            new_status = AssetOperationalStatus.INACTIVE
        else:
            new_status = AssetOperationalStatus.PARTIAL
        Asset.objects.filter(pk=self.pk).update(operational_status=new_status)

    @property
    def last_movement_transaction(self):
        """Most recent pickup (MOVEMENT) transaction for this asset, or None."""
        cached = self.prefetched_transactions()
        if cached is not None:
            return next((t for t in cached if t.transaction_type == TransactionTypeChoices.MOVEMENT), None)
        return self.transactions.filter(transaction_type=TransactionTypeChoices.MOVEMENT).order_by("-date").first()

    @property
    def verteo_pending_transaction(self):
        """The last pickup awaiting its verteo (RECEIPT), or None if there isn't one.

        Not restricted to pickups from an OBRA — a full cuba can just as
        legitimately be sitting at an ALMACEN awaiting its trip to the
        vertedero.
        """
        last_movement = self.last_movement_transaction
        if last_movement is None:
            return None
        if not last_movement.is_full_cuba:
            return None
        cached = self.prefetched_transactions()
        if cached is not None:
            already_done = any(
                t.transaction_type == TransactionTypeChoices.RECEIPT and t.date > last_movement.date
                for t in cached
            )
        else:
            already_done = self.transactions.filter(
                transaction_type=TransactionTypeChoices.RECEIPT, date__gt=last_movement.date,
            ).exists()
        if already_done:
            return None
        return last_movement

    def pending_pickup_by(self, employee):
        """The AssetPlanning to resume if `employee` is still holding this asset
        unresolved — their own MOVEMENT is still its last transaction, no DELIVERY
        since. None once it's been delivered, or if someone else holds it now."""
        last_movement = self.last_movement_transaction
        if last_movement is None or last_movement.performed_by_id != employee.id:
            return None
        if self.current_transaction_status() != AssetCurrentStatus.PICKED_UP:
            return None
        return last_movement.planning

    @property
    def capacidad(self):
        return (self.data or {}).get(AssetDataKey.CAPACIDAD, "") or ""

    def is_currently_empty(self) -> bool:
        last_tx = self.transactions.order_by("-date").first()
        if last_tx is None:
            return True
        if self.current_transaction_status(last_tx) == AssetCurrentStatus.PICKED_UP:
            return False
        return not AssetTransaction.payload_is_full(last_tx.data)

    @property
    def matricula(self):
        identifier = self.identifiers.filter(label__iexact="MATRICULA").first()
        return identifier.value if identifier else None

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
    PORTE_LOAD = "porte_load", _("Load Porte")
    PORTE_DELIVERY = "porte_delivery", _("Delivery Porte")
    MACHINE_HOURS = "machine_hours", _("Machine Hours")
    LABORER_HOURS = "laborer_hours", _("Laborer Hours")


PORTE_ACTION_TRANSACTION_TYPES = {
    PorteAction.RECOGIDA: TransactionTypeChoices.PORTE_MOVEMENT,
    PorteAction.CARGA: TransactionTypeChoices.PORTE_LOAD,
    PorteAction.ENTREGA: TransactionTypeChoices.PORTE_DELIVERY,
}


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
    reference_number = models.CharField(
        max_length=255, blank=True, default="", db_index=True,
        verbose_name=_("Reference Number"),
        help_text=_("Customer/order reference the driver enters, if any. Promoted out of `data` so it's searchable."),
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    firma = models.TextField(blank=True, null=True, verbose_name=_("Signature"))
    firma_image = models.ImageField(upload_to="asset_transactions/firmas/", blank=True, null=True, verbose_name=_("Signature Image"))
    imagen = models.ImageField(upload_to="asset_transactions/images/", blank=True, null=True, verbose_name=_("Image"))
    project_file = models.ForeignKey("management.ProjectFile", on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_transactions", verbose_name=_("Project File"))
    performed_by = models.ForeignKey("company.Employee", on_delete=models.SET_NULL, null=True, blank=True, related_name="performed_transactions")
    billing_status = models.CharField(max_length=10, choices=BillingStatus.choices, default=BillingStatus.NONE, db_index=True, verbose_name=_("Billing Status"))
    planning = models.ForeignKey("assets.AssetPlanning", on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions", verbose_name=_("Planning"))
    public_token = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        default=secrets.token_urlsafe,
        verbose_name=_("Public token"),
        help_text=_("Unguessable key for the public document link encoded in the QR."),
    )

    document_file = models.FileField(
        upload_to="asset_transactions/documents/",
        blank=True,
        editable=False,
        verbose_name=_("Document"),
    )
    document_generated_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Document generated at"),
    )

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

    # Saves touching only these never re-render the stored document: they are
    # not part of the albarán (billing_status changes in bulk admin actions).
    DOCUMENT_IRRELEVANT_FIELDS = frozenset({"billing_status", "document_file", "document_generated_at"})

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        update_fields = kwargs.get("update_fields")
        affects_document = update_fields is None or not set(update_fields) <= self.DOCUMENT_IRRELEVANT_FIELDS
        if update_fields is not None and affects_document:
            # auto_now only applies to fields being saved; the stored
            # document is checked against updated_at, so keep it current.
            kwargs["update_fields"] = {*update_fields, "updated_at"}
        super().save(*args, **kwargs)
        if is_new:
            self._sync_zones_on_porte_delivery()
        elif self.document_file and affects_document:
            from assets.documents import schedule_regeneration

            schedule_regeneration(self.pk)

    @property
    def document_url(self) -> str:
        if not self.public_token:
            return ""
        from django.conf import settings
        from django.urls import reverse

        base = settings.BACKEND_URL.rstrip("/")
        path = reverse("asset_transaction_document", args=[self.public_token])
        return f"{base}{path}"

    @classmethod
    def payload_is_full(cls, data) -> bool:
        data = data or {}
        return (
            data.get(CubaTransactionKey.ESTADO) == CubaFillState.FULL
            or data.get(CubaTransactionKey.VAS_A_VERTER) == FormAffirmative.YES
        )

    @property
    def is_full_cuba(self) -> bool:
        return self.payload_is_full(self.data)

    def get_landfill(self):
        """Landfill a pickup names, or a receipt destination when that zone is one."""
        if self.transaction_type == TransactionTypeChoices.RECEIPT:
            zone = self.to_zone if self.to_zone_id else None
            if zone is not None and zone.zone_type == WorkingZone.ZoneType.VERTEDERO:
                return zone
            return None
        if self.transaction_type != TransactionTypeChoices.MOVEMENT:
            return None
        raw_name = (self.data or {}).get(CubaTransactionKey.LANDFILL) or ""
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not name or not self.company_id:
            return None
        return (
            WorkingZone.objects.filter(
                company_id=self.company_id,
                zone_type=WorkingZone.ZoneType.VERTEDERO,
                name=name,
            )
            .order_by("pk")
            .first()
        )

    def landfill_notification_email(self, *, require_auto=True):
        if self.transaction_type != TransactionTypeChoices.MOVEMENT:
            return ""
        landfill = self.get_landfill()
        if landfill is None:
            return ""
        return landfill.receipt_notification_recipient(require_auto=require_auto)

    def queue_landfill_receipt_email(self, *, require_auto=True, reply_to=None):
        email = self.landfill_notification_email(require_auto=require_auto)
        if not email:
            return ""
        from core.tasks import send_asset_transaction_email_task

        send_asset_transaction_email_task.delay(
            self.pk, to_email=email, reply_to=reply_to,
        )
        return email

    def _sync_zones_on_porte_delivery(self):
        """An own-machine porte delivery means the machine — linked via
        AssetPlanning.machine (separate from this transaction's own asset,
        the cuba it travels in) — and, if the plan says the operator(s) moved
        with it, those employees too, are now at to_zone. Keeps
        Asset/Employee.current_working_zone in sync with what actually
        happened in the field. Scoped to own-machine transport only — other
        porte service types don't track a zone this way.
        """
        if self.transaction_type != TransactionTypeChoices.PORTE_DELIVERY:
            return
        if not self.planning_id or not self.to_zone_id:
            return
        planning = self.planning
        if planning.service_type != SERVICE_TYPE_MAQUINA_PROPIA:
            return
        if planning.machine_id:
            planning.machine.current_working_zone = self.to_zone
            planning.machine.save(update_fields=["current_working_zone"])
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
    AUTOPORTE_MAQUINA = SERVICE_TYPE_AUTOPORTE_MAQUINA, _("Machine Self-Transport")
    MOVIMIENTO_PEONES = SERVICE_TYPE_PEONES, _("Laborer Movement")


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
    CAMBIO_MISMA_CUBA = "cambio_misma_cuba", _("Same Container Swap")


# Operation types whose destination-zone pickup must be resolved before the
# planning can close, and that also require a dropoff back at that zone.
# CAMBIO swaps in a different physical asset for the one picked up.
# CAMBIO_AGUA and CAMBIO_MISMA_CUBA pick up and drop off the very same asset
# at the obra — the truck must arrive empty, see requires_empty_truck_pickup().
# CAMBIO_MISMA_CUBA may need a verteo between pickup and dropoff when full.
CUBA_TWO_LEG_OPERATION_TYPES = frozenset({
    CubaOperationType.CAMBIO,
    CubaOperationType.CAMBIO_AGUA,
    CubaOperationType.CAMBIO_MISMA_CUBA,
})

# Same-asset round trips: there's no second physical asset to swap in, so the
# pickup can never be resolved via a cuba the driver is still carrying from an
# earlier job — see AssetPlanning.requires_empty_truck_pickup(). RETIRADA picks
# up a specific cuba at the client's site, so it needs the same rule.
CUBA_REQUIRES_EMPTY_TRUCK_TYPES = frozenset({
    CubaOperationType.CAMBIO_MISMA_CUBA,
    CubaOperationType.CAMBIO_AGUA,
    CubaOperationType.RETIRADA,
})

OWN_CUBA_PICKUP_TRANSACTION_TYPES = (
    TransactionTypeChoices.MOVEMENT,
    TransactionTypeChoices.PORTE_MOVEMENT,
)

CUBA_LEG_TRANSACTION_TYPES = (
    *OWN_CUBA_PICKUP_TRANSACTION_TYPES,
    TransactionTypeChoices.DELIVERY,
)


class CubaSize(models.TextChoices):
    M3_3 = "3m3", _("3 M3")
    M3_7 = "7m3", _("7 M3")
    M3_10 = "10m3", _("10 M3")
    M3_12 = "12m3", _("12 M3")
    M3_20 = "20m3", _("20 M3")
    M3_30 = "30m3", _("30 M3")
    AGUA = "agua", _("Water")
    BANERA = "banera", _("Bathtub")


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
    SERVICE_TYPE_AUTOPORTE_MAQUINA: "#6366F1",
    SERVICE_TYPE_PEONES: "#F97316",
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


# Planning types in the driver's daily task queue (GET /plannings/next/, abandon).
DRIVER_TASK_PLANNING_TYPES = [PlanningType.CUBA, PlanningType.PORTE]


# Shown as the schedule grid block's left border stripe (AssetPlanning.status_color) —
# distinct from a plain in_transit block, an open verteo needs the driver's attention.
VERTEO_PENDING_COLOR = "#D946EF"

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
    reserved_asset = models.ForeignKey(
        Asset, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="planning_reservations", verbose_name=_("Reserved Asset"),
        help_text=_(
            "Empty cuba held until the driver scans a QR. Cleared if they scan "
            "a different cuba, cancel, or the daily cleanup runs."
        ),
    )
    machine = models.ForeignKey(
        Asset, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="machine_plannings", verbose_name=_("Machine"),
        limit_choices_to={"asset_type__category": AssetTypeCategory.MACHINE},
        help_text=_("Own-machine transport only — the machine being carried, separate from the cuba used to transport it."),
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
    abandoned_reason = models.TextField(blank=True, verbose_name=_("Abandoned Reason"))
    abandoned_by = models.ForeignKey(
        'company.Employee', on_delete=models.SET_NULL, null=True, blank=True,
        related_name="abandoned_plannings", verbose_name=_("Abandoned By"),
    )
    driver_claimed_cuba = models.BooleanField(
        default=False,
        verbose_name=_("Driver Claimed Cuba"),
        help_text=_(
            "True once the driver explicitly claimed this plan's cuba — an empty "
            "pickup, or 'usar esta cuba' / a QR scan of a cuba already on the truck. "
            "Dispatch pre-assigning an asset does not set this, so a leftover cuba "
            "is never treated as already collected."
        ),
    )
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

    def reset_execution_state_for_schedule_clone(self):
        """Fresh schedule block — drop yesterday's driver-side progress and the asset."""
        self.status = PlanningStatus.PENDING
        self.driver_claimed_cuba = False
        self.abandoned_stage = ""
        self.abandoned_at = None
        self.abandoned_reason = ""
        self.abandoned_by = None
        self.asset = None
        self.reserved_asset = None

    @staticmethod
    def normalize_cuba_size(value: str) -> str:
        value = value.strip().lower()
        leading_digits = ""
        for ch in value:
            if not ch.isdigit():
                break
            leading_digits += ch
        return leading_digits or value

    def matches_cuba_size(self, capacidad: str) -> bool:
        if not self.cuba_size:
            return True
        if not capacidad:
            return False
        return self.normalize_cuba_size(capacidad) == self.normalize_cuba_size(self.cuba_size)

    @classmethod
    def locked_cuba_ids(cls, company, exclude_planning_id=None) -> set[int]:
        open_plannings = cls.objects.filter(
            company=company,
            status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
            scheduled_date=timezone.localdate(),
        )
        if exclude_planning_id is not None:
            open_plannings = open_plannings.exclude(pk=exclude_planning_id)
        ids = set(open_plannings.exclude(asset_id=None).values_list("asset_id", flat=True))
        ids |= set(
            open_plannings.filter(asset_id__isnull=True)
            .exclude(reserved_asset_id=None)
            .values_list("reserved_asset_id", flat=True)
        )
        return ids

    def release_reservation_if_scanned_other(self, scanned_asset) -> bool:
        if not self.reserved_asset_id or self.reserved_asset_id == scanned_asset.pk:
            return False
        self.reserved_asset = None
        return True

    def cancel_cuba_reservation(self):
        if self.reserved_asset_id is None:
            return self
        self.reserved_asset = None
        self.save(update_fields=["reserved_asset"])
        return self

    def save(self, *args, **kwargs):
        # Auto-tag assignment_origin unless the caller explicitly set it
        # (TransportAssigner does, to mark AUTO) — see assets/services/transport_assigner.py.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields = list(update_fields)
            kwargs["update_fields"] = update_fields
        if not self.asset_id and self.driver_claimed_cuba:
            self.driver_claimed_cuba = False
            if update_fields is not None and "driver_claimed_cuba" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "driver_claimed_cuba"]
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
        if "machine" in required and not self.machine_id:
            raise ValidationError({"machine": _("Machine is required for this service type.")})
        if "machine" not in required:
            self.machine = None

    def requires_empty_truck_pickup(self) -> bool:
        return self.cuba_operation_type in CUBA_REQUIRES_EMPTY_TRUCK_TYPES

    def mark_cuba_claimed_by_driver(self):
        if self.driver_claimed_cuba:
            return
        self.driver_claimed_cuba = True
        self.save(update_fields=["driver_claimed_cuba"])

    def has_own_cuba_pickup(self) -> bool:
        prefetched = getattr(self, "prefetched_leg_transactions", None)
        if prefetched is not None:
            return any(
                transaction.transaction_type in OWN_CUBA_PICKUP_TRANSACTION_TYPES
                for transaction in prefetched
            )
        return self.transactions.filter(
            transaction_type__in=OWN_CUBA_PICKUP_TRANSACTION_TYPES,
        ).exists()

    def _destination_dropoff_transactions(self):
        """DELIVERY transactions linked to this planning that resolve the
        dropoff leg (to_zone == destination, or any obra when destination isn't
        set)."""
        qs = self.transactions.filter(transaction_type=TransactionTypeChoices.DELIVERY)
        if self.destination_id:
            return qs.filter(to_zone_id=self.destination_id)
        return qs.filter(to_zone__zone_type=WorkingZone.ZoneType.OBRA)

    def _destination_pickup_transactions(self):
        """MOVEMENT transactions linked to this planning that resolve the
        pickup leg (from_zone == destination, or any obra when destination
        isn't set).

        Deliberately asset-agnostic, even for CAMBIO: the "vieja" pickup is
        very often the very same physical cuba that was just dropped off as
        "nueva" — dejar la cuba, esperar un rato, recoger esa misma para
        verterla y quedársela en el camión is normal for CAMBIO, not an edge
        case. CAMBIO_AGUA/CAMBIO_MISMA_CUBA also pick up at the obra but must
        drop the same asset back there (see requires_empty_truck_pickup())."""
        qs = self.transactions.filter(transaction_type=TransactionTypeChoices.MOVEMENT)
        if self.cuba_operation_type not in CUBA_TWO_LEG_OPERATION_TYPES:
            return qs
        if self.destination_id:
            return qs.filter(from_zone_id=self.destination_id)
        return qs.filter(from_zone__zone_type=WorkingZone.ZoneType.OBRA)

    def has_destination_dropoff(self) -> bool:
        prefetched = getattr(self, "prefetched_leg_transactions", None)
        if prefetched is not None:
            return any(self._is_destination_dropoff(transaction) for transaction in prefetched)
        return self._destination_dropoff_transactions().exists()

    def _is_destination_dropoff(self, transaction) -> bool:
        if transaction.transaction_type != TransactionTypeChoices.DELIVERY:
            return False
        if self.destination_id:
            return transaction.to_zone_id == self.destination_id
        zone = transaction.to_zone
        return zone is not None and zone.zone_type == WorkingZone.ZoneType.OBRA

    def _is_destination_pickup(self, transaction) -> bool:
        if self.cuba_operation_type not in CUBA_TWO_LEG_OPERATION_TYPES:
            return True
        if self.destination_id:
            return transaction.from_zone_id == self.destination_id
        zone = transaction.from_zone
        return zone is not None and zone.zone_type == WorkingZone.ZoneType.OBRA

    def _destination_pickup_transaction(self):
        """The MOVEMENT transaction that resolves this plan's pickup leg — the
        most recent one matching, in case an earlier attempt was redone."""
        prefetched = getattr(self, "prefetched_leg_transactions", None)
        if prefetched is not None:
            return next(
                (
                    transaction for transaction in prefetched
                    if transaction.transaction_type == TransactionTypeChoices.MOVEMENT
                    and self._is_destination_pickup(transaction)
                ),
                None,
            )
        return self._destination_pickup_transactions().order_by("-date").first()

    def _pickup_has_receipt(self, pickup_tx) -> bool:
        prefetched = getattr(pickup_tx, "prefetched_receipt_links", None)
        if prefetched is not None:
            return bool(prefetched)
        return pickup_tx.receipt_links.exists()

    @classmethod
    def attach_destination_pickups(cls, plannings) -> None:
        """One prefetch for the list, instead of a pickup query per planning."""
        pending = [
            planning for planning in plannings
            if planning.pk and not hasattr(planning, "prefetched_leg_transactions")
        ]
        if not pending:
            return
        prefetch_related_objects(pending, Prefetch(
            "transactions",
            queryset=(
                AssetTransaction.objects.filter(transaction_type__in=CUBA_LEG_TRANSACTION_TYPES)
                .select_related("from_zone", "to_zone")
                .prefetch_related(Prefetch("receipt_links", to_attr="prefetched_receipt_links"))
                .order_by("-date")
            ),
            to_attr="prefetched_leg_transactions",
        ))

    def has_resolved_pickup(self) -> bool:
        """An empty pickup resolves itself. A full one only resolves once ITS
        OWN verteo is registered — not just any verteo anywhere else on this
        planning. A planning can rack up an unrelated receipt earlier (e.g. a
        prep pickup at an almacén, for cambio/cambio_agua's leg 1) that has
        nothing to do with THIS pickup; checking "any receipt exists on the
        planning" would wrongly treat that as resolving this one too. Tying it
        to AssetTransactionReceipt.movement_transaction — set by ReceiptStrategy
        to the pickup it actually verted — avoids that."""
        pickup_tx = self._destination_pickup_transaction()
        if pickup_tx is None:
            return False
        if not pickup_tx.is_full_cuba:
            return True
        return self._pickup_has_receipt(pickup_tx)

    def has_pending_verteo(self) -> bool:
        """True once a full pickup has been logged for this plan but its own
        verteo hasn't — drives the horario schedule's distinct 'en tránsito,
        verteo pendiente' block color."""
        if self.planning_type != PlanningType.CUBA:
            return False
        pickup_tx = self._destination_pickup_transaction()
        if pickup_tx is None or not pickup_tx.is_full_cuba:
            return False
        return not self._pickup_has_receipt(pickup_tx)

    def is_cuba_operation_complete(self) -> bool:
        dropoff_done = self.has_destination_dropoff()
        pickup_done = self.has_resolved_pickup()
        if self.cuba_operation_type == CubaOperationType.PUESTA:
            return dropoff_done
        if self.cuba_operation_type in CUBA_TWO_LEG_OPERATION_TYPES:
            return dropoff_done and pickup_done
        return pickup_done

    def _checklist_step(self, key, label, done):
        return {
            "key": key,
            "label": label,
            "status": ChecklistStepStatus.DONE if done else ChecklistStepStatus.PENDING,
        }

    def cuba_checklist(self):
        if self.planning_type != PlanningType.CUBA or not self.cuba_operation_type:
            return []

        pickup_tx = self._destination_pickup_transaction()
        dropoff_done = self.has_destination_dropoff()
        zone_name = self.destination.name if self.destination_id else "la obra"

        def verter_step():
            label = "Verter (solo si sale llena)"
            if pickup_tx is None:
                return self._checklist_step(ChecklistStepKey.VERTER, label, False)
            if not pickup_tx.is_full_cuba:
                return {
                    "key": ChecklistStepKey.VERTER,
                    "label": label,
                    "status": ChecklistStepStatus.SKIPPED,
                }
            return self._checklist_step(
                ChecklistStepKey.VERTER, label, self._pickup_has_receipt(pickup_tx),
            )

        checklist = [self._checklist_step(
            ChecklistStepKey.RECOGER_CUBA, "Recoger cuba",
            self.driver_claimed_cuba or self.has_own_cuba_pickup(),
        )]

        op = self.cuba_operation_type
        if op == CubaOperationType.PUESTA:
            checklist.append(self._checklist_step(ChecklistStepKey.DEJAR, f"Dejar en {zone_name}", dropoff_done))
        elif op == CubaOperationType.RETIRADA:
            checklist.append(self._checklist_step(ChecklistStepKey.RECOGER, f"Recoger en {zone_name}", pickup_tx is not None))
            checklist.append(verter_step())
        elif op == CubaOperationType.CAMBIO_MISMA_CUBA:
            checklist.append(self._checklist_step(ChecklistStepKey.RECOGER, f"Recoger en {zone_name}", pickup_tx is not None))
            checklist.append(verter_step())
            checklist.append(self._checklist_step(ChecklistStepKey.DEJAR, f"Dejar en {zone_name}", dropoff_done))
        elif op == CubaOperationType.CAMBIO_AGUA:
            checklist.append(self._checklist_step(ChecklistStepKey.RECOGER, f"Recoger en {zone_name}", pickup_tx is not None))
            checklist.append(self._checklist_step(ChecklistStepKey.DEJAR, f"Dejar en {zone_name}", dropoff_done))
        elif op == CubaOperationType.CAMBIO:
            checklist.append(self._checklist_step(ChecklistStepKey.DEJAR_NUEVA, f"Dejar la cuba nueva en {zone_name}", dropoff_done))
            checklist.append(self._checklist_step(ChecklistStepKey.RECOGER_VIEJA, f"Recoger la cuba vieja en {zone_name}", pickup_tx is not None))
            checklist.append(verter_step())
        return checklist

    def porte_checklist(self):
        if self.planning_type != PlanningType.PORTE:
            return []
        is_machine = self.service_type == SERVICE_TYPE_MAQUINA_PROPIA
        pickup_done = self.driver_claimed_cuba or self.transactions.filter(
            transaction_type=TransactionTypeChoices.PORTE_MOVEMENT,
        ).exists()
        load_done = self.transactions.filter(
            transaction_type=TransactionTypeChoices.PORTE_LOAD,
        ).exists()
        dropoff_done = self.transactions.filter(
            transaction_type=TransactionTypeChoices.PORTE_DELIVERY,
        ).exists()
        origin_name = self.origin.name if self.origin_id else "el almacén"
        destination_name = self.destination.name if self.destination_id else "destino"
        carga_label = "Escanear máquina" if is_machine else f"Cargar material en {origin_name}"
        return [
            self._checklist_step(ChecklistStepKey.RECOGER, "Recoger cuba vacía", pickup_done),
            self._checklist_step(ChecklistStepKey.CARGA, carga_label, load_done),
            self._checklist_step(ChecklistStepKey.ENTREGA, f"Entrega en {destination_name}", dropoff_done),
        ]

    def porte_progress_flags(self):
        by_key = {step["key"]: step["status"] for step in self.porte_checklist()}
        done = ChecklistStepStatus.DONE
        return {
            "pickup_done": by_key.get(ChecklistStepKey.RECOGER) == done,
            "load_done": by_key.get(ChecklistStepKey.CARGA) == done,
        }

    def suppresses_pending_dropoff_warning(self, pending_dropoff):
        if pending_dropoff is None:
            return False
        same_asset = self.asset_id == pending_dropoff["asset_id"]
        same_plan = self.id == pending_dropoff["planning_id"]
        if same_asset and self.driver_claimed_cuba:
            return True
        if (
            same_plan
            and self.cuba_operation_type in CUBA_REQUIRES_EMPTY_TRUCK_TYPES
            and not pending_dropoff["verteo_pending"]
        ):
            return True
        asset = Asset.objects.filter(pk=pending_dropoff["asset_id"]).first()
        last_mv = asset.last_movement_transaction if asset is not None else None
        if (
            same_plan
            and last_mv is not None
            and not last_mv.is_full_cuba
            and not pending_dropoff["verteo_pending"]
        ):
            return True
        return False

    def can_claim_pending_cuba_for_next_task(self, pending_dropoff):
        return bool(
            pending_dropoff is not None
            and not pending_dropoff["verteo_pending"]
            and not self.requires_empty_truck_pickup()
            and not self.driver_claimed_cuba
            and (self.asset_id is None or self.asset_id == pending_dropoff["asset_id"])
        )

    def sync_cuba_operation_status(self):
        if self.planning_type != PlanningType.CUBA:
            return
        if self.status in (
            PlanningStatus.DELIVERED,
            PlanningStatus.NOT_COMPLETED,
            PlanningStatus.CANCELLED,
        ):
            return
        self.status = (
            PlanningStatus.DELIVERED
            if self.is_cuba_operation_complete()
            else PlanningStatus.IN_TRANSIT
        )
        self.save(update_fields=["status"])

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

    @classmethod
    def attach_conflict_flags(cls, plannings) -> None:
        """Batch version of has_conflict — one query for the whole list instead of one per planning."""
        candidates = [
            p for p in plannings
            if p.scheduled_time and (p.assigned_to_id or p.transport_asset_id)
            and (p.scheduled_end_time or _default_end_time(p.scheduled_time)) is not None
        ]
        if not candidates:
            return

        dates = {p.scheduled_date for p in candidates}
        driver_ids = {p.assigned_to_id for p in candidates if p.assigned_to_id}
        truck_ids = {p.transport_asset_id for p in candidates if p.transport_asset_id}

        q = Q()
        if driver_ids:
            q |= Q(assigned_to_id__in=driver_ids)
        if truck_ids:
            q |= Q(transport_asset_id__in=truck_ids)

        others_by_date = defaultdict(list)
        others = cls.objects.filter(
            q, scheduled_date__in=dates,
            status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT, PlanningStatus.NOT_COMPLETED],
        ).only("scheduled_date", "scheduled_time", "estimated_duration", "assigned_to_id", "transport_asset_id")
        for other in others:
            others_by_date[other.scheduled_date].append(other)

        for p in candidates:
            p_end = p.scheduled_end_time or _default_end_time(p.scheduled_time)
            conflict = False
            for other in others_by_date.get(p.scheduled_date, ()):
                if other.pk == p.pk:
                    continue
                if not (
                    (other.assigned_to_id and other.assigned_to_id == p.assigned_to_id)
                    or (other.transport_asset_id and other.transport_asset_id == p.transport_asset_id)
                ):
                    continue
                if not other.scheduled_time:
                    continue
                other_end = other.scheduled_end_time or _default_end_time(other.scheduled_time)
                if other_end is None:
                    continue
                if p.scheduled_time < other_end and other.scheduled_time < p_end:
                    conflict = True
                    break
            p._has_conflict = conflict

    @property
    def has_conflict(self) -> bool:
        """Does this (already scheduled) planning overlap another one for the same driver/truck, same day?

        Uses the cached value from attach_conflict_flags when available.
        """
        if not self.scheduled_time or not (self.assigned_to_id or self.transport_asset_id):
            return False
        end = self.scheduled_end_time or _default_end_time(self.scheduled_time)
        if end is None:
            return False
        cached = getattr(self, "_has_conflict", None)
        if cached is not None:
            return cached
        return type(self).has_overlap(
            scheduled_date=self.scheduled_date,
            start=self.scheduled_time,
            end=end,
            assigned_to_id=self.assigned_to_id,
            transport_asset_id=self.transport_asset_id,
            exclude_pk=self.pk,
        )

    @property
    def traceability(self):
        """``(key, label)``: the key stays the same in every language."""
        if not self.assigned_to_id or not self.transport_asset_id or not self.scheduled_time:
            return "pending", _("Pending")
        if self.has_conflict:
            return "conflict", _("Conflict")
        if self.assignment_origin == AssignmentOrigin.AUTO:
            return "auto", _("Auto-assigned")
        return "manual", _("Manual")

    @property
    def traceability_label(self):
        return self.traceability[1]

    @property
    def service_type_color(self) -> str:
        return SERVICE_TYPE_COLORS.get(self.service_type, DEFAULT_SERVICE_TYPE_COLOR)

    @property
    def urgency_color(self) -> str:
        return URGENCY_COLORS.get(self.urgency, URGENCY_COLORS[PlanningUrgency.MEDIUM])

    @property
    def status_color(self) -> str:
        return self.status_color_given(self.has_pending_verteo_in_transit)

    @property
    def has_pending_verteo_in_transit(self) -> bool:
        """In transit with its verteo still pending: the schedule's distinct block state.
        Costs a query or two; callers showing several fields of it compute it once."""
        return self.status == PlanningStatus.IN_TRANSIT and self.has_pending_verteo()

    def status_color_given(self, pending_verteo_in_transit: bool) -> str:
        if pending_verteo_in_transit:
            return VERTEO_PENDING_COLOR
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
        """Where a driver left this job — stored when they skip or abandon it."""
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
    machine = models.ForeignKey(
        Asset, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="machine_planning_requests", verbose_name=_("Machine"),
        limit_choices_to={"asset_type__category": AssetTypeCategory.MACHINE},
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="asset_planning_requests",
        verbose_name=_("Project File"),
        help_text=_("Not applicable to planning_type == NONE requests (e.g. Laborer/Machinist Movement)."),
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

    # planning_type == NONE requests only (Machinist Movement, Machine
    # Self-Transport — see apply_zone_change()): when the zone change applied.
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
        "assigned_to_id", "asset_id", "machine_id", "transport_asset_id", "project_file_id",
    ]

    def save(self, *args, **kwargs):
        previous_status = None
        if self.pk:
            previous = AssetPlanningRequest.objects.filter(pk=self.pk).values(
                "status", *self.REVIEW_TRACKED_FIELDS
            ).first()
            if previous:
                previous_status = previous["status"]
                # Only auto-revert when this save doesn't already carry an explicit
                # status change (i.e. it's a plain edit, not the approve/reject action).
                if self.status != AssetPlanningRequestStatus.PENDING_REVIEW and previous_status == self.status and any(
                    getattr(self, field) != previous[field] for field in self.REVIEW_TRACKED_FIELDS
                ):
                    self.status = AssetPlanningRequestStatus.PENDING_REVIEW
                    self.reviewed_by = None
                    self.reviewed_at = None
                    self.rejection_reason = ""
        with transaction.atomic():
            super().save(*args, **kwargs)
            self._sync_zone_sync_schedule(previous_status)

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

        # asset is a generic optional field (no service type currently
        # requires it) — never auto-cleared, unlike machine/cuba_operation_type
        # below, which are exclusive to specific service types.
        if "asset" in required and not self.asset_id:
            raise ValidationError({"asset": _("Asset is required for this service type.")})
        if self.asset_id and self.company_id and self.asset.company_id != self.company_id:
            raise ValidationError({"asset": _("This asset belongs to a different company than the selected zone.")})

        if "machine" in required and not self.machine_id:
            raise ValidationError({"machine": _("Machine is required for this service type.")})
        if "machine" not in required:
            self.machine = None
        elif self.machine_id and self.company_id and self.machine.company_id != self.company_id:
            raise ValidationError({"machine": _("This machine belongs to a different company than the selected zone.")})

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
        # that dict and has no estimated_duration field at all. No route to
        # estimate for planning_type == NONE requests (nothing gets dispatched).
        if self.planning_type == PlanningType.NONE:
            self.estimated_duration = None
        elif not self.estimated_duration:
            raise ValidationError({"estimated_duration": _("Estimated duration is required for this service type.")})

        if self.service_type != SERVICE_TYPE_MAQUINA_PROPIA:
            self.machinist_moved = False

        # Optional for every service type: a suggestion the route assignment
        # algorithm (or the reviewer) fills in or overrides if left blank.
        if self.assigned_to_id and self.company_id and self.assigned_to.company_id != self.company_id:
            raise ValidationError({"assigned_to": _("This employee belongs to a different company than the selected zone.")})

        # planning_type == NONE requests never produce an AssetPlanning.
        if self.planning_type != PlanningType.NONE and not self.project_file_id:
            raise ValidationError({"project_file": _("Project File is required.")})
        if self.project_file_id and self.company_id and self.project_file.company_id != self.company_id:
            raise ValidationError({"project_file": _("This project file belongs to a different company than the selected zone.")})

    def get_estimated_duration(self):
        from assets.services.route_optimizer import GoogleRouteOptimizer
        return GoogleRouteOptimizer().estimate_duration(self.from_zone, self.to_zone)

    @property
    def is_zone_sync_due(self) -> bool:
        """Whether needed_date/needed_time has arrived — only meaningful for
        planning_type == NONE requests, see apply_zone_change()."""
        if self.zone_sync_applied_at is not None:
            return False
        now = timezone.localtime()
        if self.needed_date > now.date():
            return False
        if self.needed_date == now.date() and self.needed_time and self.needed_time > now.time():
            return False
        return True

    def apply_zone_change(self) -> bool:
        """Move this request's asset (if any) and every selected employee's
        current_working_zone to to_zone, then mark this applied. Only
        meaningful for planning_type == NONE requests. Locked so a concurrent
        call can't double-apply."""
        with transaction.atomic():
            locked = AssetPlanningRequest.objects.select_for_update().get(pk=self.pk)
            if locked.zone_sync_applied_at is not None:
                return False
            if locked.asset_id:
                locked.asset.current_working_zone = locked.to_zone
                locked.asset.save()
            for employee in locked.moved_employees.all():
                employee.current_working_zone = locked.to_zone
                employee.save()
            locked.zone_sync_applied_at = timezone.now()
            locked.save(update_fields=["zone_sync_applied_at"])
        self.zone_sync_applied_at = locked.zone_sync_applied_at
        return True

    ZONE_SYNC_TASK_NAME_PREFIX = "assets.zone_sync_request."
    ZONE_SYNC_TASK_PATH = "assets.tasks.apply_scheduled_zone_change"

    @property
    def zone_sync_task_name(self) -> str:
        return f"{self.ZONE_SYNC_TASK_NAME_PREFIX}{self.pk}"

    def _sync_zone_sync_schedule(self, previous_status: str | None) -> None:
        """Apply/schedule/cancel the zone-sync task based on approval
        transitions. Only planning_type == NONE requests need one."""
        if self.planning_type != PlanningType.NONE:
            return

        became_approved = (
            self.status == AssetPlanningRequestStatus.APPROVED
            and previous_status != AssetPlanningRequestStatus.APPROVED
        )
        left_approved = (
            previous_status == AssetPlanningRequestStatus.APPROVED
            and self.status != AssetPlanningRequestStatus.APPROVED
        )
        if became_approved:
            if self.is_zone_sync_due:
                self.apply_zone_change()
            else:
                self._schedule_zone_sync()
        elif left_approved:
            self._cancel_zone_sync()

    def _schedule_zone_sync(self) -> None:
        """One-off task, fired once at needed_date/needed_time — no polling."""
        from django_celery_beat.models import ClockedSchedule, PeriodicTask

        run_at = timezone.make_aware(datetime.combine(self.needed_date, self.needed_time or time.min))
        clocked, _created = ClockedSchedule.objects.get_or_create(clocked_time=run_at)
        PeriodicTask.objects.update_or_create(
            name=self.zone_sync_task_name,
            defaults={
                "task": self.ZONE_SYNC_TASK_PATH,
                "clocked": clocked,
                "interval": None,
                "crontab": None,
                "args": json.dumps([self.pk]),
                "one_off": True,
                "enabled": True,
            },
        )

    def _cancel_zone_sync(self) -> None:
        from django_celery_beat.models import PeriodicTask

        task = PeriodicTask.objects.filter(name=self.zone_sync_task_name).select_related("clocked").first()
        if not task:
            return
        clocked = task.clocked
        task.delete()
        if clocked and not PeriodicTask.objects.filter(clocked=clocked).exists():
            clocked.delete()


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
    machine = models.ForeignKey(
        Asset, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="machine_planning_request_drafts", verbose_name=_("Machine"),
        limit_choices_to={"asset_type__category": AssetTypeCategory.MACHINE},
    )
    load_description = models.TextField(blank=True, verbose_name=_("Load Description"))
    # SERVICE_TYPE_PEONES only — the laborers to move from from_zone to to_zone.
    moved_employees = models.ManyToManyField(
        'company.Employee', blank=True, related_name="laborer_moved_request_drafts",
        verbose_name=_("Moved Employees"),
    )
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
            value = getattr(self, f"{field_name}_id" if field_name in ("asset", "machine") else field_name)
            if not value:
                missing.append(field_name)
        # planning_type == NONE requests aren't tied to a client site.
        needs_client = SERVICE_TYPE_PLANNING_TYPE.get(self.service_type) != PlanningType.NONE
        if needs_client and not self.client_id and not self.new_client_name.strip():
            missing.append("client")
        if not self.to_zone_id and not self.destination_text.strip():
            missing.append("destination_text")
        if "from_zone" in needed and not self.from_zone_id and not self.origin_text.strip():
            missing.append("origin_text")
        if self.service_type in SERVICE_TYPES_NEEDING_MOVED_EMPLOYEES and (
            self.pk is None or not self.moved_employees.exists()
        ):
            missing.append("moved_employees")
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


class AssetFaultType(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))

    class Meta:
        verbose_name = _("Asset Fault Type")
        verbose_name_plural = _("Asset Fault Types")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.company})"


class AssetFault(CompanyOwnedModel, Timestampable):
    asset = models.ForeignKey(
        Asset, on_delete=models.CASCADE, related_name="faults",
        verbose_name=_("Asset"),
    )
    fault_type = models.ForeignKey(
        AssetFaultType, on_delete=models.PROTECT,
        verbose_name=_("Fault Type"),
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    severity = models.CharField(
        max_length=10, choices=AssetFaultSeverity.choices,
        verbose_name=_("Severity"),
    )
    status = models.CharField(
        max_length=10, choices=AssetFaultStatus.choices,
        default=AssetFaultStatus.PENDING,
        verbose_name=_("Status"),
    )

    class Meta:
        verbose_name = _("Asset Fault")
        verbose_name_plural = _("Asset Faults")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.fault_type} — {self.asset} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if not self.company_id and self.asset:
            self.company = self.asset.company
        super().save(*args, **kwargs)
        self.asset.recalculate_operational_status()

    def delete(self, *args, **kwargs):
        asset = self.asset
        super().delete(*args, **kwargs)
        asset.recalculate_operational_status()