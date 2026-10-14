"""
Manual-correction path for the admin "Create Transaction" wizard: creates an
AssetTransaction from an AssetPlanning when the mobile app's automatic flow
never ran (phone failure, skipped step, etc.).

One strategy class per transaction_type, mirroring the mobile app's own split
(api.fleet.views.assets.BaseTransactionStrategy and its subclasses;
api.fleet.views.attendance.PorteSubmitView) -- same field resolution and side
effects, minus the mobile-only concerns (GPS, QR-scan gating, photo capture).
"""
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from assets.models import (
    AssetTransaction,
    AssetTransactionReceipt,
    CubaFillState,
    CubaTransactionKey,
    PlanningStatus,
    TransactionTypeChoices,
)


class BaseManualTransactionStrategy:
    transaction_type = None

    def __init__(self, *, transaction_type, planning, zone=None, performed_by=None, notes="", data=None, asset=None):
        self.transaction_type = transaction_type
        self.planning = planning
        self.zone = zone
        self.performed_by = performed_by
        self.notes = notes
        self.data = data or {}
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
            project_file=project_file,
            planning=self.planning,
            notes=self.notes,
            data=self.data,
        )

    def _require_asset(self):
        asset = self.asset_override or self.planning.asset
        if asset is None:
            raise ValueError(_("This planning has no asset assigned yet. Pick one in the Asset field above."))
        return asset


class MovementStrategy(BaseManualTransactionStrategy):
    def execute(self):
        asset = self._require_asset()
        from_zone = asset.current_working_zone
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
        to_zone = self.zone or self.planning.destination
        if to_zone is None:
            raise ValueError(_("No destination zone: choose one, or set a destination on the planning."))
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
        nro_albaran = (self.data.get("nro_albaran") or "").strip()
        if not nro_albaran:
            raise ValueError(_("An albaran number is required to register a receipt (verteo)."))

        # Same lookup the mobile ReceiptStrategy uses: the most recent pickup,
        # to link the receipt and inherit its project_file.
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
        AssetTransactionReceipt.objects.create(
            transaction=transaction,
            nro_albaran=self.data.get("nro_albaran", ""),
            data={"material_vertido": self.data.get("material_vertido", "")},
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
            data={CubaTransactionKey.ESTADO: CubaFillState.EMPTY},
        )


class PorteStrategy(BaseManualTransactionStrategy):
    """Covers all three porte transaction types (recogida/carga/entrega) --
    they only differ in which asset gets used on delivery and in the
    planning status update, not in field resolution."""

    def execute(self):
        asset = self._resolve_asset()
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
        if self.transaction_type == TransactionTypeChoices.PORTE_DELIVERY and self.planning.machine_id:
            asset = self.planning.machine
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


def create_manual_transaction(*, planning, transaction_type, zone=None, performed_by=None, notes="", data=None, asset=None):
    strategy_class = MANUAL_TRANSACTION_STRATEGY_MAP.get(transaction_type)
    if strategy_class is None:
        raise ValueError(
            _("create_manual_transaction: unsupported transaction_type %(transaction_type)r")
            % {"transaction_type": transaction_type}
        )
    strategy = strategy_class(
        transaction_type=transaction_type, planning=planning, zone=zone,
        performed_by=performed_by, notes=notes, data=data, asset=asset,
    )
    return strategy.execute()
