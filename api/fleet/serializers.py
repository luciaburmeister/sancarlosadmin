from rest_framework import serializers
from assets.constants import SERVICE_TYPE_MAQUINA_PROPIA
from assets.models import AssetTransaction, CubaTransactionKey, TransactionTypeChoices
from assets.serializers import MachineAssetSerializer
from api.fleet.utils import get_zone_coordinates


class AssetTransactionSerializer(serializers.Serializer):
    """
    Validates the body of POST /assets/<id>/transaction/.

    By declaring transaction_type as a ChoiceField pointing at
    TransactionTypeChoices, DRF automatically rejects any value that
    is not in the choices list — no hardcoded strings needed in the view.
    """
    transaction_type = serializers.ChoiceField(
        choices=TransactionTypeChoices.choices
    )
    # GPS coordinates — used by movement and delivery
    latitude = serializers.FloatField(required=False, default=0)
    longitude = serializers.FloatField(required=False, default=0)

    # Firma and imagen — used by movement and delivery
    firma = serializers.CharField(required=False, default="", allow_blank=True)
    imagen = serializers.CharField(required=False, default="", allow_blank=True)

    # Albaran fields — used by movement and delivery
    # No default so they are excluded from validated_data when not sent
    matricula = serializers.CharField(required=False, allow_blank=True)
    tipo_camion = serializers.CharField(required=False, allow_blank=True)
    implemento = serializers.CharField(required=False, allow_blank=True)
    cliente = serializers.CharField(required=False, allow_blank=True)
    obra = serializers.CharField(required=False, allow_blank=True)
    trabajo = serializers.CharField(required=False, allow_blank=True)
    tipo_cuba = serializers.CharField(required=False, allow_blank=True)
    m3 = serializers.CharField(required=False, allow_blank=True)
    vertedero = serializers.CharField(required=False, allow_blank=True)

    # Receipt-specific fields — no default so they are excluded from
    # validated_data when not sent (prevents them polluting movement/delivery data)
    nro_albaran = serializers.CharField(required=False, allow_blank=True)
    foto_albaran = serializers.CharField(required=False, allow_blank=True)
    peso_neto = serializers.CharField(required=False, allow_blank=True)
    material_vertido = serializers.CharField(required=False, allow_blank=True)

    # Zone override — sent when driver picks from the multiple-zones dialog
    zone_id = serializers.IntegerField(required=False, default=None, allow_null=True)

    # Machine hours fields
    action = serializers.ChoiceField(
        choices=["start", "pause", "resume", "end"],
        required=False,
        allow_null=True,
        default=None,
    )


class CubaCandidateSerializer(serializers.Serializer):
    asset_id = serializers.IntegerField(source="asset.id")
    name = serializers.CharField(source="asset.name")
    capacidad = serializers.SerializerMethodField()
    zone_id = serializers.SerializerMethodField()
    zone_name = serializers.SerializerMethodField()
    distance_m = serializers.FloatField(allow_null=True)

    def get_capacidad(self, candidate):
        return candidate.asset.capacidad or None

    def get_zone_id(self, candidate):
        zone = candidate.asset.current_working_zone
        return zone.id if zone else None

    def get_zone_name(self, candidate):
        zone = candidate.asset.current_working_zone
        return zone.name if zone else None


class CubaReservationRequestSerializer(serializers.Serializer):
    asset_id = serializers.IntegerField()


class CubaReservationResultSerializer(serializers.Serializer):
    reserved_asset_id = serializers.IntegerField(source="reserved_asset.id")
    reserved_asset_name = serializers.CharField(source="reserved_asset.name")
    zone_name = serializers.SerializerMethodField()
    planning_id = serializers.IntegerField(source="id")

    def get_zone_name(self, planning):
        zone = planning.reserved_asset.current_working_zone if planning.reserved_asset else None
        return zone.name if zone else None


class CubaPlanningStopSerializer(serializers.Serializer):
    zone = serializers.CharField(source="zone.name")
    address = serializers.CharField(source="zone.full_address")
    sequence = serializers.IntegerField()


class ChecklistStepSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    status = serializers.CharField()


class CubaPlanningSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    planning_type = serializers.CharField()
    asset_id = serializers.SerializerMethodField()
    asset_name = serializers.CharField(source="asset.name", allow_null=True, default=None)
    origin = serializers.CharField(source="origin.name", allow_null=True, default=None)
    origin_address = serializers.CharField(source="origin.full_address", allow_null=True, default=None)
    origin_lat = serializers.SerializerMethodField()
    origin_lon = serializers.SerializerMethodField()
    destination = serializers.CharField(source="destination.name", allow_null=True, default=None)
    destination_address = serializers.CharField(source="destination.full_address", allow_null=True, default=None)
    destination_lat = serializers.SerializerMethodField()
    destination_lon = serializers.SerializerMethodField()
    stops = CubaPlanningStopSerializer(many=True)
    transport_asset = MachineAssetSerializer(allow_null=True)
    transport_asset_matricula = serializers.CharField(
        source="transport_asset.matricula", allow_null=True, default=None,
    )
    route_order = serializers.IntegerField(allow_null=True)
    scheduled_date = serializers.DateField(format="%d/%m/%Y")
    status = serializers.CharField()
    notes = serializers.CharField(allow_blank=True, allow_null=True)
    service_type = serializers.CharField(allow_blank=True, allow_null=True)
    service_type_label = serializers.SerializerMethodField()
    cuba_operation_type = serializers.CharField(allow_blank=True, allow_null=True)
    cuba_operation_type_label = serializers.SerializerMethodField()
    cuba_size = serializers.CharField(allow_blank=True, allow_null=True)
    cuba_size_label = serializers.SerializerMethodField()
    cuba_capacidad = serializers.SerializerMethodField()
    reserved_asset_id = serializers.IntegerField(allow_null=True)
    reserved_asset_name = serializers.CharField(
        source="reserved_asset.name", allow_null=True, default=None,
    )
    reserved_asset_zone_name = serializers.CharField(
        source="reserved_asset.current_working_zone.name", allow_null=True, default=None,
    )
    reserved_asset_zone_address = serializers.CharField(
        source="reserved_asset.current_working_zone.full_address", allow_null=True, default=None,
    )
    reserved_asset_zone_lat = serializers.SerializerMethodField()
    reserved_asset_zone_lon = serializers.SerializerMethodField()
    checklist = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._coord_cache = {}

    def _coords(self, zone):
        key = zone.pk if zone is not None else None
        if key not in self._coord_cache:
            self._coord_cache[key] = get_zone_coordinates(zone) if zone else (None, None)
        return self._coord_cache[key]

    def get_asset_id(self, planning):
        return planning.asset_id or 0

    def get_origin_lat(self, planning):
        return self._coords(planning.origin)[0]

    def get_origin_lon(self, planning):
        return self._coords(planning.origin)[1]

    def get_destination_lat(self, planning):
        return self._coords(planning.destination)[0]

    def get_destination_lon(self, planning):
        return self._coords(planning.destination)[1]

    def get_service_type_label(self, planning):
        return planning.get_service_type_display() if planning.service_type else None

    def get_cuba_operation_type_label(self, planning):
        return planning.get_cuba_operation_type_display() if planning.cuba_operation_type else None

    def get_cuba_size_label(self, planning):
        return planning.get_cuba_size_display() if planning.cuba_size else None

    def get_cuba_capacidad(self, planning):
        return (planning.asset.capacidad or None) if planning.asset else None

    def get_reserved_asset_zone_lat(self, planning):
        zone = planning.reserved_asset.current_working_zone if planning.reserved_asset else None
        return self._coords(zone)[0]

    def get_reserved_asset_zone_lon(self, planning):
        zone = planning.reserved_asset.current_working_zone if planning.reserved_asset else None
        return self._coords(zone)[1]

    def get_checklist(self, planning):
        return ChecklistStepSerializer(planning.cuba_checklist(), many=True).data


class PortePlanningSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    planning_type = serializers.CharField()
    asset_id = serializers.SerializerMethodField()
    asset_name = serializers.SerializerMethodField()
    asset_assigned = serializers.SerializerMethodField()
    load_description = serializers.CharField(allow_blank=True, allow_null=True)
    origin = serializers.CharField(source="origin.name", allow_null=True, default=None)
    origin_address = serializers.CharField(source="origin.full_address", allow_null=True, default=None)
    origin_lat = serializers.SerializerMethodField()
    origin_lon = serializers.SerializerMethodField()
    destination = serializers.CharField(source="destination.name", allow_null=True, default=None)
    destination_address = serializers.CharField(source="destination.full_address", allow_null=True, default=None)
    destination_lat = serializers.SerializerMethodField()
    destination_lon = serializers.SerializerMethodField()
    scheduled_date = serializers.DateField(format="%d/%m/%Y")
    status = serializers.CharField()
    notes = serializers.CharField(allow_blank=True, allow_null=True)
    transport_asset = MachineAssetSerializer(allow_null=True)
    transport_asset_matricula = serializers.CharField(
        source="transport_asset.matricula", allow_null=True, default=None,
    )
    service_type = serializers.CharField(allow_blank=True, allow_null=True)
    service_type_label = serializers.SerializerMethodField()
    is_machine_porte = serializers.SerializerMethodField()
    machine_id = serializers.SerializerMethodField()
    machine_name = serializers.CharField(source="machine.name", allow_null=True, default=None)
    pickup_done = serializers.SerializerMethodField()
    load_done = serializers.SerializerMethodField()
    checklist = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._coord_cache = {}
        self._checklist_cache = {}
        self._progress_cache = {}

    def _coords(self, zone):
        key = zone.pk if zone is not None else None
        if key not in self._coord_cache:
            self._coord_cache[key] = get_zone_coordinates(zone) if zone else (None, None)
        return self._coord_cache[key]

    def _display_asset(self, planning):
        return planning.asset or planning.transport_asset

    def _checklist(self, planning):
        if planning.pk not in self._checklist_cache:
            self._checklist_cache[planning.pk] = planning.porte_checklist()
        return self._checklist_cache[planning.pk]

    def _progress(self, planning):
        if planning.pk not in self._progress_cache:
            self._progress_cache[planning.pk] = planning.porte_progress_flags()
        return self._progress_cache[planning.pk]

    def get_asset_id(self, planning):
        scannable_asset_id = self.context.get("scannable_asset_id")
        if scannable_asset_id is not None:
            return scannable_asset_id
        display_asset = self._display_asset(planning)
        return display_asset.id if display_asset else 0

    def get_asset_name(self, planning):
        display_asset = self._display_asset(planning)
        return display_asset.name if display_asset else ""

    def get_asset_assigned(self, planning):
        return planning.asset_id is not None

    def get_origin_lat(self, planning):
        return self._coords(planning.origin)[0]

    def get_origin_lon(self, planning):
        return self._coords(planning.origin)[1]

    def get_destination_lat(self, planning):
        return self._coords(planning.destination)[0]

    def get_destination_lon(self, planning):
        return self._coords(planning.destination)[1]

    def get_service_type_label(self, planning):
        return planning.get_service_type_display() if planning.service_type else None

    def get_is_machine_porte(self, planning):
        return planning.service_type == SERVICE_TYPE_MAQUINA_PROPIA

    def get_machine_id(self, planning):
        return planning.machine_id or 0

    def get_pickup_done(self, planning):
        return self._progress(planning)["pickup_done"]

    def get_load_done(self, planning):
        return self._progress(planning)["load_done"]

    def get_checklist(self, planning):
        return ChecklistStepSerializer(self._checklist(planning), many=True).data


class AssetListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField(source="name")
    description = serializers.CharField(source="asset_type.name", default="")
    status = serializers.SerializerMethodField()
    asset_receipt = serializers.BooleanField(source="has_pending_receipt")
    vas_a_verter = serializers.SerializerMethodField()
    last_cuba_estado = serializers.SerializerMethodField()
    last_location = serializers.SerializerMethodField()
    last_updated = serializers.SerializerMethodField()
    zone_address = serializers.CharField(source="current_working_zone.full_address", allow_null=True, default=None)
    zone_lat = serializers.SerializerMethodField()
    zone_lon = serializers.SerializerMethodField()
    cliente = serializers.CharField(source="client.name", default="")
    obra = serializers.SerializerMethodField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_tx_cache = {}
        self._zone_coord_cache = {}

    def _last_transaction(self, asset):
        if asset.pk not in self._last_tx_cache:
            self._last_tx_cache[asset.pk] = asset.transactions.order_by("-date").first()
        return self._last_tx_cache[asset.pk]

    def _zone_coords(self, asset):
        zone = asset.current_working_zone
        key = zone.pk if zone is not None else None
        if key not in self._zone_coord_cache:
            self._zone_coord_cache[key] = get_zone_coordinates(zone) if zone else (None, None)
        return self._zone_coord_cache[key]

    def get_status(self, asset):
        return asset.current_transaction_status(self._last_transaction(asset))

    def get_vas_a_verter(self, asset):
        return asset.verteo_pending_transaction is not None

    def get_last_cuba_estado(self, asset):
        last_state_tx = AssetTransaction.objects.filter(
            asset=asset,
            transaction_type__in=[TransactionTypeChoices.MOVEMENT, TransactionTypeChoices.DELIVERY],
        ).order_by("-date").first()
        return (last_state_tx.data.get(CubaTransactionKey.ESTADO) or None) if last_state_tx else None

    def get_last_location(self, asset):
        zone = asset.current_working_zone
        return str(zone) if zone else None

    def get_last_updated(self, asset):
        last_tx = self._last_transaction(asset)
        return last_tx.date.strftime("%d/%m/%Y %H:%M") if last_tx else None

    def get_zone_lat(self, asset):
        return self._zone_coords(asset)[0]

    def get_zone_lon(self, asset):
        return self._zone_coords(asset)[1]

    def get_obra(self, asset):
        zone = asset.current_working_zone
        return str(zone) if zone else ""
