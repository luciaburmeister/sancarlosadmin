"""
Manual-correction path for the admin "Create Transaction" wizard: creates an
AssetTransaction from an AssetPlanning when the mobile app's automatic flow
never ran (phone failure, skipped step, etc.).

One strategy class per transaction_type, mirroring the mobile app's own split
(api.fleet.views.assets.BaseTransactionStrategy and its subclasses;
api.fleet.views.attendance.PorteSubmitView) -- same field resolution and side
effects, minus the mobile-only concerns (GPS, QR-scan gating, photo capture).
"""
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.models import (
    Asset,
    AssetIdentifierLabel,
    AssetTransaction,
    AssetTransactionReceipt,
    CubaFillState,
    CubaTransactionKey,
    CubaWorkType,
    PlanningStatus,
    PorteTransactionDataKey,
    TransactionTypeChoices,
)


class BaseManualTransactionStrategy:
    transaction_type = None

    def __init__(self, *, transaction_type, planning, zone=None, performed_by=None, notes="", data=None, asset=None, reference_number=""):
        self.transaction_type = transaction_type
        self.planning = planning
        self.zone = zone
        self.performed_by = performed_by
        self.notes = notes
        self.data = data or {}
        self.reference_number = reference_number or ""
        # Explicit pick from the wizard, for a planning that has no asset/
        # transport_asset/machine of its own yet (e.g. the mobile app's QR-scan
        # assignment step never ran) -- takes priority over whatever the
        # planning itself carries, since the user chose it deliberately.
        self.asset_override = asset

    def execute(self) -> AssetTransaction:
        raise NotImplementedError

    def _create_transaction(self, *, asset, from_zone, to_zone, project_file):
        return AssetTransaction.objects.create(
            asset=asset,
            company=asset.company,
            from_zone=from_zone,
            to_zone=to_zone,
            transaction_type=self.transaction_type,
            date=timezone.now(),
            performed_by=self.performed_by,
            project_file=AssetTransaction.resolve_project_file(self.planning, project_file),
            planning=self.planning,
            reference_number=self.reference_number,
            notes=self.notes,
            data=self.data,
        )

    def _set_if_blank(self, key, value):
        if value and not (self.data.get(key) or ""):
            self.data[key] = value

    def _stamp_planning_id(self):
        self.data[PorteTransactionDataKey.PLANNING_ID] = self.planning.pk

    def _stamp_zone(self, zone):
        client_name = ""
        zone_type = ""
        if zone is not None:
            client_name = zone.client.name if zone.client_id else ""
            zone_type = zone.zone_type or ""
        self.data[CubaTransactionKey.CLIENT_NAME] = client_name
        self.data[CubaTransactionKey.ZONE_TYPE] = zone_type
        # The emailed albarán prints data.obra with no fallback.
        site = ""
        if zone is not None:
            site = zone.full_address or zone.name
        self._set_if_blank(CubaTransactionKey.SITE, site)

    def _stamp_observations(self):
        self._set_if_blank(CubaTransactionKey.OBSERVATIONS, (self.notes or "").strip())

    def _stamp_truck(self):
        """Plate, truck type and implement, the same three the app copies off
        the truck when the driver confirms a pickup or drop-off."""
        planning_plate = self.planning.transport_plate()
        self._set_if_blank(PorteTransactionDataKey.MATRICULA, planning_plate)
        plate = (self.data.get(PorteTransactionDataKey.MATRICULA) or "").strip()
        truck = self._truck_for_plate(plate) if plate else None
        if truck is None and (not plate or plate == planning_plate):
            truck = self.planning.transport_asset
        if truck is None:
            return
        truck_data = truck.data or {}
        self._set_if_blank(CubaTransactionKey.TRUCK_TYPE, truck_data.get(CubaTransactionKey.TRUCK_TYPE) or "")
        self._set_if_blank(CubaTransactionKey.IMPLEMENT, truck_data.get(CubaTransactionKey.IMPLEMENT) or "")

    def _truck_for_plate(self, plate):
        return (
            Asset.objects.filter(
                company_id=self.planning.company_id,
                identifiers__label__iexact=AssetIdentifierLabel.PLATE,
                identifiers__value__iexact=plate,
            )
            .order_by("pk")
            .first()
        )

    def _require_asset(self):
        asset = self.asset_override or self.planning.asset
        if asset is None:
            raise ValueError(_("This planning has no asset assigned yet. Pick one in the Asset field above."))
        return asset


class MovementStrategy(BaseManualTransactionStrategy):
    def execute(self):
        asset = self._require_asset()
        self.data = dict(self.data)
        self.data[CubaTransactionKey.WORK] = CubaWorkType.PICKUP
        self._set_if_blank(CubaTransactionKey.M3, self.planning.recorded_capacity(asset))
        self._stamp_truck()
        self._stamp_observations()
        from_zone = asset.current_working_zone
        self._stamp_planning_id()
        self._stamp_zone(from_zone)
        project_file = from_zone.project_file if from_zone else None
        transaction = self._create_transaction(
            asset=asset, from_zone=from_zone, to_zone=None, project_file=project_file,
        )
        self.planning.sync_cuba_operation_status()
        transaction.queue_landfill_receipt_email()
        return transaction


class DeliveryStrategy(BaseManualTransactionStrategy):
    def execute(self):
        asset = self._require_asset()
        self.data = dict(self.data)
        self.data[CubaTransactionKey.WORK] = CubaWorkType.DROPOFF
        self._set_if_blank(CubaTransactionKey.M3, self.planning.recorded_capacity(asset))
        self._stamp_truck()
        self._stamp_observations()
        to_zone = self.zone or self.planning.destination
        if to_zone is None:
            raise ValueError(_("No destination zone: choose one, or set a destination on the planning."))
        self._stamp_planning_id()
        self._stamp_zone(to_zone)
        transaction = self._create_transaction(
            asset=asset, from_zone=asset.current_working_zone, to_zone=to_zone,
            project_file=to_zone.project_file,
        )
        asset.current_working_zone = to_zone
        asset.save(update_fields=["current_working_zone"])
        self.planning.sync_cuba_operation_status()
        return transaction


class ReceiptStrategy(BaseManualTransactionStrategy):
    def execute(self):
        asset = self._require_asset()
        nro_albaran = (self.data.get(CubaTransactionKey.ALBARAN) or "").strip()
        if not nro_albaran:
            raise ValueError(_("An albaran number is required to register a receipt (verteo)."))
        self.data = dict(self.data)
        self._stamp_planning_id()

        # Same lookup the mobile ReceiptStrategy uses, to link the receipt.
        # Its project_file is the fallback when the plan has none.
        movement_tx = AssetTransaction.objects.filter(
            asset=asset, transaction_type=TransactionTypeChoices.MOVEMENT,
        ).order_by("-date").first()

        transaction = self._create_transaction(
            asset=asset, from_zone=asset.current_working_zone, to_zone=self.zone,
            project_file=movement_tx.project_file if movement_tx else None,
        )
        self._create_receipt(asset, transaction, movement_tx)
        self.planning.sync_cuba_operation_status()
        return transaction

    def _create_receipt(self, asset, transaction, movement_tx):
        receipt_data = {CubaTransactionKey.MATERIAL_DUMPED: self.data.get(CubaTransactionKey.MATERIAL_DUMPED, "")}
        peso_neto = (self.data.get(CubaTransactionKey.NET_WEIGHT) or "").strip()
        if peso_neto:
            receipt_data[CubaTransactionKey.NET_WEIGHT] = peso_neto
        AssetTransactionReceipt.objects.create(
            transaction=transaction,
            nro_albaran=self.data.get(CubaTransactionKey.ALBARAN, ""),
            data=receipt_data,
            movement_transaction=movement_tx,
        )
        self._maybe_repickup(asset, transaction, movement_tx)

    def _maybe_repickup(self, asset, transaction, movement_tx):
        # A verteo of an already-delivered cuba implies an unrecorded pickup
        # first -- register it so status tracking doesn't think it's still there.
        was_delivered_since_pickup = movement_tx is not None and AssetTransaction.objects.filter(
            asset=asset, transaction_type=TransactionTypeChoices.DELIVERY, date__gt=movement_tx.date,
        ).exists()
        if not was_delivered_since_pickup:
            return
        AssetTransaction.objects.create(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=None,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            performed_by=self.performed_by,
            project_file=transaction.project_file,
            planning=self.planning,
            notes=_("Manual correction: implicit pickup registered at verteo (was already delivered)."),
            data={
                CubaTransactionKey.ESTADO: CubaFillState.EMPTY,
                PorteTransactionDataKey.PLANNING_ID: self.planning.pk,
            },
        )


class PorteStrategy(BaseManualTransactionStrategy):
    """Covers all three porte transaction types (recogida/carga/entrega) --
    they only differ in which asset gets used on delivery and in the
    planning status update, not in field resolution."""

    def execute(self):
        asset = self._resolve_asset()
        self.data = dict(self.data)
        self.data.setdefault(PorteTransactionDataKey.LOAD_DESCRIPTION, self.planning.load_description or "")
        effective_machine = self.planning.effective_machine()
        self.data.setdefault(PorteTransactionDataKey.MACHINE_ID, effective_machine.id if effective_machine else None)
        self.data.setdefault(PorteTransactionDataKey.CLIENT_ID, None)
        self._set_if_blank(PorteTransactionDataKey.MATRICULA, self.planning.transport_plate())
        self._stamp_planning_id()
        transaction = self._create_transaction(
            asset=asset, from_zone=self.planning.origin, to_zone=self.planning.destination,
            project_file=self._project_file(),
        )
        self._update_planning_status()
        self.planning.sync_cuba_operation_status()
        return transaction

    def _resolve_asset(self):
        # An explicit pick from the wizard always wins -- the user chose it
        # deliberately, so skip the delivery-vs-machine auto-resolution below.
        if self.asset_override is not None:
            return self.asset_override
        # Mirrors PorteSubmitView: the asset/truck, except on delivery for a
        # machine-porte, where the assigned machine is what's being delivered.
        machine = self.planning.effective_machine() if self.transaction_type == TransactionTypeChoices.PORTE_DELIVERY else None
        if machine is not None:
            asset = machine
        else:
            asset = self.planning.asset or self.planning.transport_asset
        if asset is None:
            raise ValueError(_("This planning has no asset, machine, or transport truck assigned yet. Pick one in the Asset field above."))
        return asset

    def _project_file(self):
        destination, origin = self.planning.destination, self.planning.origin
        return (destination.project_file if destination else None) or (origin.project_file if origin else None)

    def _update_planning_status(self):
        # Mirrors PorteSubmitView: entrega closes the planning out, any other step marks it in transit.
        if self.transaction_type == TransactionTypeChoices.PORTE_DELIVERY:
            self.planning.status = PlanningStatus.DELIVERED
            self.planning.save(update_fields=["status"])
        elif self.planning.status != PlanningStatus.DELIVERED:
            self.planning.status = PlanningStatus.IN_TRANSIT
            self.planning.save(update_fields=["status"])


MANUAL_TRANSACTION_STRATEGY_MAP = {
    TransactionTypeChoices.MOVEMENT: MovementStrategy,
    TransactionTypeChoices.DELIVERY: DeliveryStrategy,
    TransactionTypeChoices.RECEIPT: ReceiptStrategy,
    TransactionTypeChoices.PORTE_MOVEMENT: PorteStrategy,
    TransactionTypeChoices.PORTE_LOAD: PorteStrategy,
    TransactionTypeChoices.PORTE_DELIVERY: PorteStrategy,
}


def create_manual_transaction(*, planning, transaction_type, zone=None, performed_by=None, notes="", data=None, asset=None, reference_number=""):
    strategy_class = MANUAL_TRANSACTION_STRATEGY_MAP.get(transaction_type)
    if strategy_class is None:
        raise ValueError(
            _("create_manual_transaction: unsupported transaction_type %(transaction_type)r")
            % {"transaction_type": transaction_type}
        )
    strategy = strategy_class(
        transaction_type=transaction_type, planning=planning, zone=zone,
        performed_by=performed_by, notes=notes, data=data, asset=asset,
        reference_number=reference_number,
    )
    with db_transaction.atomic():
        return strategy.execute()
