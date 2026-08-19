import base64
import io
import json
import uuid
import logging
from datetime import datetime
import qrcode
from company.models import WorkingZone, WorkingZoneAuthorization
from django.core.files.base import ContentFile
from assets.models import Asset, AssetTransaction, TransactionTypeChoices, AssetTransactionReceipt, AssetPlanning, AssetTransactionPhoto, MachinePermission, PlanningStatus, PlanningType, AssignmentOrigin
from core.tasks import send_asset_transaction_email_task
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from api.constants import MAX_ZONE_RADIUS_M, FIRST_TRANSACTION_RADIUS_M
from api.fleet.serializers import AssetTransactionSerializer
from api.fleet.utils import find_zone_within_radius, get_zone_coordinates, haversine_distance, find_zones_within_radius, confirm_zone_location
from company.utils import get_current_employee


def _base64_to_image_file(b64_string):
    if not b64_string:
        return None
    try:
        if "," in b64_string:
            b64_string = b64_string.split(",", 1)[1]
        image_data = base64.b64decode(b64_string)
        return ContentFile(image_data, name=f"{uuid.uuid4()}.jpg")
    except Exception:
        return None


def _save_transaction_photo(transaction, b64_string, photo_type):
    photo_file = _base64_to_image_file(b64_string)
    if photo_file:
        try:
            photo = AssetTransactionPhoto(transaction=transaction, photo_type=photo_type)
            photo.photo.save(photo_file.name, photo_file, save=True)
        except Exception:
            pass


def _now_iso():
    return timezone.now().isoformat()


def _today_str():
    return str(timezone.now().date())


def _parse_dt(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


def _format_time(iso_str):
    dt = _parse_dt(iso_str)
    return dt.strftime("%H:%M") if dt else None


# ── QuerySet helpers using has_key (confirmed working) ────────────────────────

def _open_machine_qs(asset, employee, today):
    qs = AssetTransaction.objects.filter(
        performed_by=employee,
        transaction_type=TransactionTypeChoices.MACHINE_HOURS,
        data__machine_start_time__startswith=today,
    ).exclude(data__has_key='machine_end_time')
    if asset is not None:
        qs = qs.filter(asset=asset)
    return qs


def _closed_machine_qs(asset, employee, today):
    qs = AssetTransaction.objects.filter(
        performed_by=employee,
        transaction_type=TransactionTypeChoices.MACHINE_HOURS,
        data__machine_start_time__startswith=today,
        data__has_key='machine_end_time',
    )
    if asset is not None:
        qs = qs.filter(asset=asset)
    return qs


def _open_laborer_qs(employee, today):
    return AssetTransaction.objects.filter(
        performed_by=employee,
        transaction_type=TransactionTypeChoices.LABORER_HOURS,
        data__laborer_start_time__startswith=today,
    ).exclude(data__has_key='laborer_end_time')


def get_asset_receipt(asset):
    last_movement = AssetTransaction.objects.filter(asset=asset, transaction_type=TransactionTypeChoices.MOVEMENT).order_by("-date").first()
    if last_movement is None:
        return False
    last_receipt = AssetTransaction.objects.filter(asset=asset, transaction_type=TransactionTypeChoices.RECEIPT).order_by("-date").first()
    if last_receipt is None:
        return False
    return last_receipt.date > last_movement.date



def _serialize_asset_list_item(asset, last_tx, zone, zone_coords):
    # vas_a_verter: calculated inline — no need for a separate API helper
    last_movement = AssetTransaction.objects.filter(
        asset=asset,
        transaction_type=TransactionTypeChoices.MOVEMENT,
    ).order_by("-date").first()
    if (
        last_movement is not None
        and last_movement.data.get("vasAVerter", "") == "Sí"
        and not AssetTransaction.objects.filter(
            asset=asset,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date__gt=last_movement.date,
        ).exists()
    ):
        vas_a_verter = True
    else:
        vas_a_verter = False

    return {
        "id": asset.id,
        "code": asset.name,
        "description": asset.asset_type.name if asset.asset_type else "",
        "status": asset.current_transaction_status(last_tx),
        "asset_receipt": asset.has_pending_receipt,
        "vas_a_verter": vas_a_verter,
        "last_location": str(zone) if zone else None,
        "last_updated": last_tx.date.strftime("%d/%m/%Y %H:%M") if last_tx else None,
        "zone_address": f"{zone.street_address}, {zone.city}, {zone.postal_code}" if zone else None,
        "zone_lat": zone_coords[0],
        "zone_lon": zone_coords[1],
        "cliente": asset.client.name if asset.client else "",
        "obra": str(zone) if zone else "",
    }


def asset_transaction_imagen_view(request, transaction_id):
    try:
        transaction = AssetTransaction.objects.get(id=transaction_id)
    except AssetTransaction.DoesNotExist:
        return HttpResponse("Not found", status=404)
    if not transaction.imagen:
        return HttpResponse("No image", status=404)
    image_data = base64.b64decode(transaction.imagen)
    return HttpResponse(image_data, content_type="image/jpeg")


class AssetsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"assets": []})
        assets = Asset.objects.select_related("asset_type", "current_working_zone").filter(assigned_to=employee)
        data = []
        for asset in assets:
            last_tx = asset.transactions.order_by("-date").first()
            zone = asset.current_working_zone
            zone_coords = get_zone_coordinates(zone) if zone else (None, None)
            data.append(_serialize_asset_list_item(asset, last_tx, zone, zone_coords))
        return JsonResponse({"assets": data})


class AssetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, asset_id):
        user = request.user
        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "Asset not found"}, status=404)
        try:
            asset = Asset.objects.select_related("asset_type", "current_working_zone").get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)
        if asset.company != employee.company:
            return JsonResponse({"error": "Asset not found"}, status=404)
        last_tx = asset.transactions.order_by("-date").first()
        zone = asset.current_working_zone
        zone_coords = get_zone_coordinates(zone) if zone else (None, None)
        return JsonResponse({
            **_serialize_asset_list_item(asset, last_tx, zone, zone_coords),
            "assigned_to": asset.assigned_to.user.username if asset.assigned_to and asset.assigned_to.user else None,
            "assigned_to_me": asset.assigned_to == employee,
            "obra": str(asset.current_working_zone) if asset.current_working_zone else "",
        })


class AssetAssignView(APIView):
    """Driver self-assignment via QR scan — scanning a cuba is the claim: it always
    hands the asset AND its today's planning to the scanning driver, taking it from
    whoever had it before if needed. There's no manual dispatch assignment to
    conflict with, so the physical scan is treated as ground truth.

    Cubas aren't required to be pre-pinned to a plan — that's the point of scanning:
    if this driver has their own pending cuba plan today with no asset picked yet,
    the scanned cuba fills it in, same as BaseTransactionStrategy._require_planning."""
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        user = request.user
        try:
            asset = Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)
        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        if asset.company != employee.company:
            return JsonResponse({"error": "Este cubo no pertenece a tu empresa"}, status=403)

        today = timezone.now().date()
        base_qs = AssetPlanning.objects.filter(
            company=asset.company, planning_type=PlanningType.CUBA,
            status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
            scheduled_date=today,
        )
        planning = base_qs.filter(asset=asset).order_by("scheduled_time", "route_order", "pk").first()
        if planning is None:
            unassigned = base_qs.filter(
                asset__isnull=True, assigned_to=employee,
            ).order_by("scheduled_time", "route_order", "pk").first()
            if unassigned is not None:
                unassigned.asset = asset
                unassigned.save(update_fields=["asset"])
                planning = unassigned
        if planning is None:
            return JsonResponse({"error": "No hay ningún plan pendiente hoy para esta cuba."}, status=400)

        asset.assigned_to = employee
        asset.save(update_fields=["assigned_to"])
        if planning.assigned_to_id != employee.id:
            planning.assigned_to = employee
            planning.assignment_origin = AssignmentOrigin.MANUAL
            planning.save(update_fields=["assigned_to", "assignment_origin"])
        return JsonResponse({
            "success": True,
            "message": f"Cubo {asset.name} asignado a {employee.full_name}",
            "planning_id": planning.id,
        })


class AssetQRView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, asset_id):
        try:
            Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return HttpResponse("Asset not found", status=404)
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(f"cubosapp://container/{asset_id}")
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return HttpResponse(buffer, content_type="image/png")


class BaseTransactionStrategy:
    def __init__(self, data: dict, asset: Asset, employee, user):
        self.data = data
        self.asset = asset
        self.employee = employee
        self.user = user

    def _save_firma_and_imagen(self, transaction):
        firma = self.data.get("firma", "")
        imagen = self.data.get("imagen", "")
        if firma:
            transaction.firma = firma
        image_file = _base64_to_image_file(imagen)
        if image_file:
            transaction.imagen.save(image_file.name, image_file, save=False)

    def _require_planning(self):
        """An asset can't be operated without an active plan assigned to the driver for that asset."""
        planning_id = self.data.get("planning_id")
        if not planning_id:
            return None, JsonResponse({"error": "Debes tener un plan asignado para poder operar."}, status=400)
        base_qs = AssetPlanning.objects.filter(
            id=planning_id,
            company=self.asset.company,
            assigned_to=self.employee,
            status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
        )
        # First try: planning explicitly pre-assigned to this asset
        planning = base_qs.filter(asset=self.asset).first()
        # Second try: planning with no asset yet — the scanned cuba is the one being operated
        if planning is None:
            unassigned = base_qs.filter(asset__isnull=True).first()
            if unassigned is not None:
                unassigned.asset = self.asset
                unassigned.save(update_fields=["asset"])
                planning = unassigned
        if planning is None:
            return None, JsonResponse({"error": "El plan indicado no existe o no está asignado a ti."}, status=400)
        return planning, None

    def execute(self) -> JsonResponse:
        raise NotImplementedError


class MovementStrategy(BaseTransactionStrategy):
    def execute(self) -> JsonResponse:
        planning, error = self._require_planning()
        if error:
            return error
        data = self.data
        asset = self.asset
        employee = self.employee
        user = self.user
        latitude = data.get("latitude", 0)
        longitude = data.get("longitude", 0)
        form_data = {k: v for k, v in data.items() if k not in ("transaction_type", "latitude", "longitude", "firma", "imagen", "zone_id")}
        distance = None
        chosen_zone_id = data.get("zone_id")
        if chosen_zone_id:
            try:
                detected_zone = WorkingZone.objects.select_related("client").get(id=chosen_zone_id, company=asset.company)
                asset.current_working_zone = detected_zone
                asset.save(update_fields=["current_working_zone"])
            except WorkingZone.DoesNotExist:
                return JsonResponse({"error": "Zona no encontrada."}, status=400)
        elif asset.current_working_zone is not None:
            zone_lat, zone_lon = get_zone_coordinates(asset.current_working_zone)
            if zone_lat is not None:
                radius = MAX_ZONE_RADIUS_M if asset.current_working_zone.location_confirmed_at else FIRST_TRANSACTION_RADIUS_M
                distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
                if distance > radius:
                    return JsonResponse({"error": f"Estas demasiado lejos de {asset.current_working_zone}. Estas a {int(distance)}m. Debes estar a menos de {radius}m."}, status=400)
        else:
            nearby_zones = find_zones_within_radius(latitude, longitude, asset.company)
            if len(nearby_zones) == 0:
                nearby_zones = find_zones_within_radius(
                    latitude, longitude, asset.company,
                    radius_metres=FIRST_TRANSACTION_RADIUS_M, unconfirmed_only=True,
                )
            if len(nearby_zones) == 0:
                return JsonResponse({"error": "No estas cerca de ninguna zona conocida."}, status=400)
            elif len(nearby_zones) > 1:
                return JsonResponse({"multiple_zones": True, "zones": [{"id": z.id, "name": str(z)} for z, d in nearby_zones]}, status=200)
            else:
                detected_zone, distance = nearby_zones[0]
                asset.current_working_zone = detected_zone
                asset.save(update_fields=["current_working_zone"])
        zone = asset.current_working_zone
        if zone is not None and zone.location_confirmed_at is None and latitude and longitude:
            confirm_zone_location(zone, latitude, longitude)
        if zone:
            if zone.client_id and not hasattr(zone, '_client_cache'):
                zone = WorkingZone.objects.select_related("client").get(pk=zone.pk)
            form_data["cliente"] = zone.client.name if zone.client else ""
            form_data["zone_type"] = zone.zone_type
            form_data["trabajo"] = "Recogida"
        else:
            form_data["cliente"] = ""
            form_data["zone_type"] = ""
        distance_note = f"Distancia: {int(distance)}m" if distance is not None else "Sin zona asignada"
        transaction = AssetTransaction(
            asset=asset, company=asset.company, from_zone=asset.current_working_zone, to_zone=None,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(), performed_by=employee,
            project_file=asset.current_working_zone.project_file if asset.current_working_zone else None,
            planning=planning,
            notes=f"Recogido por {user.username}. GPS: {latitude}, {longitude}. {distance_note}", data=form_data,
        )
        self._save_firma_and_imagen(transaction)
        transaction.save()
        planning.status = PlanningStatus.IN_TRANSIT
        planning.save(update_fields=["status"])
        send_asset_transaction_email_task.delay(transaction.id)
        return JsonResponse({"success": True, "message": "Recogida registrada", "transaction_id": transaction.id})


class DeliveryStrategy(BaseTransactionStrategy):
    def execute(self) -> JsonResponse:
        planning, error = self._require_planning()
        if error:
            return error
        data = self.data
        asset = self.asset
        employee = self.employee
        user = self.user
        latitude = data.get("latitude", 0)
        longitude = data.get("longitude", 0)
        form_data = {k: v for k, v in data.items() if k not in ("transaction_type", "latitude", "longitude", "firma", "imagen", "zone_id")}
        chosen_zone_id = data.get("zone_id")
        if chosen_zone_id:
            try:
                destination_zone = WorkingZone.objects.select_related("client").get(id=chosen_zone_id, company=asset.company)
                distance = 0
            except WorkingZone.DoesNotExist:
                return JsonResponse({"error": "Zona no encontrada."}, status=400)
        else:
            nearby_zones = find_zones_within_radius(latitude, longitude, asset.company)
            if len(nearby_zones) == 0:
                nearby_zones = find_zones_within_radius(
                    latitude, longitude, asset.company,
                    radius_metres=FIRST_TRANSACTION_RADIUS_M, unconfirmed_only=True,
                )
            if len(nearby_zones) == 0:
                return JsonResponse({"error": "No estas cerca de ninguna zona de entrega conocida."}, status=400)
            elif len(nearby_zones) > 1:
                return JsonResponse({"multiple_zones": True, "zones": [{"id": z.id, "name": str(z)} for z, d in nearby_zones]}, status=200)
            else:
                destination_zone, distance = nearby_zones[0]
        if destination_zone.client_id and not hasattr(destination_zone, '_client_cache'):
            destination_zone = WorkingZone.objects.select_related("client").get(pk=destination_zone.pk)
        if destination_zone.location_confirmed_at is None and latitude and longitude:
            confirm_zone_location(destination_zone, latitude, longitude)
        form_data["cliente"] = destination_zone.client.name if destination_zone.client else ""
        form_data["zone_type"] = destination_zone.zone_type
        form_data["trabajo"] = "Dejada"
        transaction = AssetTransaction(
            asset=asset, company=asset.company, from_zone=asset.current_working_zone, to_zone=destination_zone,
            transaction_type=TransactionTypeChoices.DELIVERY, date=timezone.now(), performed_by=employee,
            project_file=destination_zone.project_file if destination_zone else None,
            planning=planning,
            notes=f"Entregado por {user.username} en {destination_zone}. GPS: {latitude}, {longitude}. Distancia: {int(distance)}m",
            data=form_data,
        )
        self._save_firma_and_imagen(transaction)
        transaction.save()
        asset.current_working_zone = destination_zone
        asset.save()
        matches_planned_destination = (
            planning.destination_id is None or planning.destination_id == destination_zone.id
        )
        if matches_planned_destination:
            planning.status = PlanningStatus.DELIVERED
            planning.save(update_fields=["status"])
            message = f"Entrega registrada en {destination_zone}"
        else:
            # Wrong zone: plan stays pending until delivered correctly or abandoned.
            message = (
                f"Entrega registrada en {destination_zone}, pero no es la zona planificada. "
                "Debes entregarla en la zona correcta."
            )
        send_asset_transaction_email_task.delay(transaction.id)
        return JsonResponse({"success": True, "message": message, "transaction_id": transaction.id})


class ReceiptStrategy(BaseTransactionStrategy):
    def execute(self) -> JsonResponse:
        planning, error = self._require_planning()
        if error:
            return error
        data = self.data
        asset = self.asset
        employee = self.employee
        user = self.user
        nro_albaran = data.get("nro_albaran", "").strip()
        foto_b64 = data.get("foto_albaran", "")
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        peso_neto = data.get("peso_neto", "")
        material_vertido = data.get("material_vertido", "")
        if not nro_albaran:
            return JsonResponse({"error": "El numero de albaran es obligatorio."}, status=400)
        vertedero_zone = None
        chosen_zone_id = data.get("zone_id")
        if chosen_zone_id:
            try:
                vertedero_zone = WorkingZone.objects.get(id=chosen_zone_id, company=asset.company)
            except WorkingZone.DoesNotExist:
                pass
        elif latitude is not None and longitude is not None:
            nearby_zones = find_zones_within_radius(latitude, longitude, asset.company, zone_type=WorkingZone.ZoneType.VERTEDERO)
            if len(nearby_zones) > 1:
                return JsonResponse({"multiple_zones": True, "zones": [{"id": z.id, "name": str(z)} for z, d in nearby_zones]}, status=200)
            elif len(nearby_zones) == 1:
                vertedero_zone, _ = nearby_zones[0]
        movement_tx = AssetTransaction.objects.filter(
            asset=asset, transaction_type=TransactionTypeChoices.MOVEMENT, from_zone__zone_type=WorkingZone.ZoneType.OBRA,
        ).order_by("-date").first()
        form_data = {k: v for k, v in data.items() if k not in ("transaction_type", "latitude", "longitude", "zone_id", "foto_albaran")}
        transaction = AssetTransaction(
            asset=asset, company=asset.company, from_zone=asset.current_working_zone, to_zone=vertedero_zone,
            transaction_type=TransactionTypeChoices.RECEIPT, date=timezone.now(), performed_by=employee,
            planning=planning,
            notes=f"Verteo registrado por {user.username}. Albaran: {nro_albaran}. Material: {material_vertido}. Peso: {peso_neto}kg.",
            data=form_data,
        )
        transaction.save()
        receipt = AssetTransactionReceipt(
            transaction=transaction, nro_albaran=nro_albaran,
            data={"peso_neto": peso_neto, "material_vertido": material_vertido},
            movement_transaction=movement_tx,
        )
        foto_file = _base64_to_image_file(foto_b64)
        if foto_file:
            receipt.foto.save(foto_file.name, foto_file, save=False)
        receipt.save()
        return JsonResponse({"success": True, "message": "Verteo registrado correctamente.", "transaction_id": transaction.id, "receipt_id": receipt.id})


def _validate_zone(asset, latitude, longitude, chosen_zone_id):
    if chosen_zone_id:
        try:
            zone = WorkingZone.objects.get(id=chosen_zone_id, company=asset.company)
            return zone, None
        except WorkingZone.DoesNotExist:
            return None, JsonResponse({"error": "Zona no encontrada."}, status=400)
    if asset.current_working_zone is not None:
        zone_lat, zone_lon = get_zone_coordinates(asset.current_working_zone)
        if zone_lat is not None and latitude != 0 and longitude != 0:
            distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
            if distance > MAX_ZONE_RADIUS_M:
                return None, JsonResponse({"error": f"Estas demasiado lejos de {asset.current_working_zone}. Estas a {int(distance)}m. Debes estar a menos de {MAX_ZONE_RADIUS_M}m."}, status=400)
        return asset.current_working_zone, None
    nearby_zones = find_zones_within_radius(latitude, longitude, asset.company, zone_types=[WorkingZone.ZoneType.OBRA])
    if len(nearby_zones) == 0:
        return None, JsonResponse({"error": "No estas cerca de ninguna obra conocida."}, status=400)
    elif len(nearby_zones) > 1:
        return None, JsonResponse({"multiple_zones": True, "zones": [{"id": z.id, "name": str(z)} for z, _ in nearby_zones]}, status=200)
    else:
        detected_zone, _ = nearby_zones[0]
        return detected_zone, None


class MachineHoursStrategy(BaseTransactionStrategy):

    def execute(self) -> JsonResponse:
        action = self.data.get("action", "").strip()
        if action == "start":
            return self._handle_start()
        elif action == "pause":
            return self._handle_pause()
        elif action == "resume":
            return self._handle_resume()
        elif action == "end":
            return self._handle_end()
        else:
            return JsonResponse({"error": "action must be: start, pause, resume, or end."}, status=400)

    def _now_str(self):
        return timezone.now().strftime("%H:%M:%S")

    def _handle_start(self) -> JsonResponse:
        asset = self.asset
        employee = self.employee
        user = self.user
        today = _today_str()
        latitude = self.data.get("latitude", 0)
        longitude = self.data.get("longitude", 0)
        photo_b64 = self.data.get("photo_start", "")
        chosen_zone_id = self.data.get("zone_id")
        planning_id = self.data.get("planning_id")
        planning_obj = None
        if planning_id:
            planning_obj = AssetPlanning.objects.filter(id=planning_id, company=asset.company).first()

        already_open = _open_machine_qs(asset, employee, today).exists()
        if already_open:
            return JsonResponse({"error": "Ya tienes un fichaje abierto hoy para esta maquina. Pausa o finaliza primero."}, status=400)

        detected_zone, err = _validate_zone(asset, latitude, longitude, chosen_zone_id)
        if err:
            return err

        now_str = self._now_str()

        existing_tx = _closed_machine_qs(asset, employee, today).order_by("-data__machine_start_time").first()
        if existing_tx:
            intervals = existing_tx.data.get("intervals", [])
            intervals.append({"start": now_str, "end": None})
            existing_tx.data["intervals"] = intervals
            existing_tx.data.pop("machine_end_time", None)
            existing_tx.notes += f" | Reabierto por {user.username} a las {now_str}."
            existing_tx.save(update_fields=["data", "notes"])
            _save_transaction_photo(existing_tx, photo_b64, AssetTransactionPhoto.PhotoType.START)
            return JsonResponse({"success": True, "message": "Jornada reanudada.", "transaction_id": existing_tx.id})

        transaction = AssetTransaction(
            asset=asset, company=asset.company, from_zone=detected_zone, to_zone=None,
            transaction_type=TransactionTypeChoices.MACHINE_HOURS, date=timezone.now(), performed_by=employee,
            project_file=detected_zone.project_file if detected_zone else None,
            planning=planning_obj,
            notes=f"Inicio de maquina por {user.username}. GPS: {latitude}, {longitude}.",
            data={
                "planning_id": planning_id,
                "machine_start_time": _now_iso(),
                "intervals": [{"start": now_str, "end": None}],
            },
        )
        transaction.save()
        _save_transaction_photo(transaction, photo_b64, AssetTransactionPhoto.PhotoType.START)
        return JsonResponse({"success": True, "message": "Inicio de maquina registrado.", "transaction_id": transaction.id})

    def _handle_pause(self) -> JsonResponse:
        asset = self.asset
        employee = self.employee
        user = self.user
        today = _today_str()
        tx = _open_machine_qs(asset, employee, today).first()
        if not tx:
            return JsonResponse({"error": "No hay ningún fichaje abierto para pausar."}, status=404)
        intervals = tx.data.get("intervals", [])
        if not intervals or intervals[-1].get("end") is not None:
            return JsonResponse({"error": "La maquina ya esta pausada."}, status=400)
        intervals[-1]["end"] = self._now_str()
        tx.data["intervals"] = intervals
        tx.notes += f" | Pausa por {user.username} a las {intervals[-1]['end']}."
        tx.save(update_fields=["data", "notes"])
        return JsonResponse({"success": True, "message": "Pausa registrada."})

    def _handle_resume(self) -> JsonResponse:
        asset = self.asset
        employee = self.employee
        user = self.user
        today = _today_str()
        latitude = self.data.get("latitude", 0)
        longitude = self.data.get("longitude", 0)
        chosen_zone_id = self.data.get("zone_id")
        tx = _open_machine_qs(asset, employee, today).first()
        if not tx:
            return JsonResponse({"error": "No hay ningún fichaje abierto para reanudar."}, status=404)
        intervals = tx.data.get("intervals", [])
        if intervals and intervals[-1].get("end") is None:
            return JsonResponse({"error": "La maquina ya esta en marcha."}, status=400)
        _, err = _validate_zone(asset, latitude, longitude, chosen_zone_id)
        if err:
            return err
        now_str = self._now_str()
        intervals.append({"start": now_str, "end": None})
        tx.data["intervals"] = intervals
        tx.notes += f" | Reanudado por {user.username} a las {now_str}."
        tx.save(update_fields=["data", "notes"])
        return JsonResponse({"success": True, "message": "Reanudado correctamente."})

    def _handle_end(self) -> JsonResponse:
        asset = self.asset
        employee = self.employee
        user = self.user
        today = _today_str()
        latitude = self.data.get("latitude", 0)
        longitude = self.data.get("longitude", 0)
        photo_b64 = self.data.get("photo_end", "")
        chosen_zone_id = self.data.get("zone_id")
        planning_id = self.data.get("planning_id")

        qs = _open_machine_qs(asset, employee, today)
        try:
            tx = qs.get()
        except AssetTransaction.DoesNotExist:
            return JsonResponse({"error": "No hay ningún fichaje abierto hoy para esta maquina."}, status=404)
        except AssetTransaction.MultipleObjectsReturned:
            tx = qs.order_by("-data__machine_start_time").first()

        _, err = _validate_zone(asset, latitude, longitude, chosen_zone_id)
        if err:
            return err

        now_str = self._now_str()
        intervals = tx.data.get("intervals", [])
        if intervals and intervals[-1].get("end") is None:
            intervals[-1]["end"] = now_str
        tx.data["intervals"] = intervals
        tx.data["machine_end_time"] = _now_iso()
        tx.notes += f" | Fin registrado por {user.username}. GPS: {latitude}, {longitude}."
        tx.save(update_fields=["data", "notes"])
        _save_transaction_photo(tx, photo_b64, AssetTransactionPhoto.PhotoType.END)

        if planning_id:
            try:
                AssetPlanning.objects.filter(id=planning_id).update(status=PlanningStatus.DELIVERED)
            except Exception:
                pass

        return JsonResponse({"success": True, "message": "Fin de maquina registrado.", "transaction_id": tx.id})


class LaborerHoursStrategy:
    def __init__(self, data: dict, employee, user):
        self.data = data
        self.employee = employee
        self.user = user

    def _now_str(self):
        return timezone.now().strftime("%H:%M:%S")

    def execute(self) -> JsonResponse:
        action = self.data.get("action", "").strip()
        if action == "start":
            return self._handle_start()
        elif action == "pause":
            return self._handle_pause()
        elif action == "resume":
            return self._handle_resume()
        elif action == "end":
            return self._handle_end()
        else:
            return JsonResponse({"error": "action must be: start, pause, resume, or end."}, status=400)

    def _handle_start(self) -> JsonResponse:
        employee = self.employee
        user = self.user
        today = _today_str()
        zone_id = self.data.get("zone_id")
        latitude = self.data.get("latitude", 0)
        longitude = self.data.get("longitude", 0)

        if not zone_id:
            return JsonResponse({"error": "zone_id is required."}, status=400)
        try:
            zone = WorkingZone.objects.get(pk=zone_id, company=employee.company)
        except WorkingZone.DoesNotExist:
            return JsonResponse({"error": "Zona no encontrada."}, status=404)

        is_authorized = WorkingZoneAuthorization.objects.filter(working_zone=zone, employee=employee).exists()
        if not is_authorized:
            return JsonResponse({"error": "No tienes autorización para fichar en esta obra."}, status=403)

        already_open = _open_laborer_qs(employee, today).exists()
        if already_open:
            return JsonResponse({"error": "Ya tienes un fichaje de peón abierto hoy. Pausa o finaliza primero."}, status=400)

        now_str = self._now_str()

        existing_tx = AssetTransaction.objects.filter(
            performed_by=employee,
            transaction_type=TransactionTypeChoices.LABORER_HOURS,
            data__laborer_start_time__startswith=today,
            data__has_key='laborer_end_time',
            to_zone=zone,
        ).order_by("-data__laborer_start_time").first()

        if existing_tx:
            intervals = existing_tx.data.get("intervals", [])
            intervals.append({"start": now_str, "end": None})
            existing_tx.data["intervals"] = intervals
            existing_tx.data.pop("laborer_end_time", None)
            existing_tx.notes += f" | Reabierto por {user.username} a las {now_str}."
            existing_tx.save(update_fields=["data", "notes"])
            return JsonResponse({"success": True, "message": "Fichaje reanudado.", "transaction_id": existing_tx.id, "status": "started", "zone_name": zone.name})

        transaction = AssetTransaction(
            asset=None, company=employee.company, to_zone=zone,
            transaction_type=TransactionTypeChoices.LABORER_HOURS, date=timezone.now(), performed_by=employee,
            project_file=zone.project_file if zone else None,
            notes=f"Inicio hora de peón por {user.username} en {zone}. GPS: {latitude}, {longitude}.",
            data={
                "laborer_start_time": _now_iso(),
                "intervals": [{"start": now_str, "end": None}],
                "latitude": latitude,
                "longitude": longitude,
            },
        )
        transaction.save()
        return JsonResponse({"success": True, "message": "Fichaje de peón iniciado.", "transaction_id": transaction.id, "status": "started", "zone_name": zone.name})

    def _handle_pause(self) -> JsonResponse:
        employee = self.employee
        user = self.user
        today = _today_str()
        tx = _open_laborer_qs(employee, today).first()
        if not tx:
            return JsonResponse({"error": "No hay ningún fichaje de peón abierto para pausar."}, status=404)
        intervals = tx.data.get("intervals", [])
        if not intervals or intervals[-1].get("end") is not None:
            return JsonResponse({"error": "El fichaje ya está pausado."}, status=400)
        now_str = self._now_str()
        intervals[-1]["end"] = now_str
        tx.data["intervals"] = intervals
        tx.notes += f" | Pausa por {user.username} a las {now_str}."
        tx.save(update_fields=["data", "notes"])
        return JsonResponse({"success": True, "message": "Pausa registrada.", "status": "paused"})

    def _handle_resume(self) -> JsonResponse:
        employee = self.employee
        user = self.user
        today = _today_str()
        tx = _open_laborer_qs(employee, today).first()
        if not tx:
            return JsonResponse({"error": "No hay ningún fichaje de peón abierto para reanudar."}, status=404)
        intervals = tx.data.get("intervals", [])
        if intervals and intervals[-1].get("end") is None:
            return JsonResponse({"error": "El fichaje ya está en marcha."}, status=400)
        now_str = self._now_str()
        intervals.append({"start": now_str, "end": None})
        tx.data["intervals"] = intervals
        tx.notes += f" | Reanudado por {user.username} a las {now_str}."
        tx.save(update_fields=["data", "notes"])
        return JsonResponse({"success": True, "message": "Reanudado correctamente.", "status": "started"})

    def _handle_end(self) -> JsonResponse:
        employee = self.employee
        user = self.user
        today = _today_str()
        latitude = self.data.get("latitude", 0)
        longitude = self.data.get("longitude", 0)
        qs = _open_laborer_qs(employee, today)
        try:
            tx = qs.get()
        except AssetTransaction.DoesNotExist:
            return JsonResponse({"error": "No hay ningún fichaje de peón abierto hoy."}, status=404)
        except AssetTransaction.MultipleObjectsReturned:
            tx = qs.order_by("-data__laborer_start_time").first()
        now_str = self._now_str()
        intervals = tx.data.get("intervals", [])
        if intervals and intervals[-1].get("end") is None:
            intervals[-1]["end"] = now_str
        tx.data["intervals"] = intervals
        tx.data["laborer_end_time"] = _now_iso()
        tx.notes += f" | Fin registrado por {user.username}. GPS: {latitude}, {longitude}."
        tx.save(update_fields=["data", "notes"])
        return JsonResponse({"success": True, "message": "Fichaje de peón finalizado.", "transaction_id": tx.id, "status": "ended"})


TRANSACTION_STRATEGY_MAP = {
    TransactionTypeChoices.MOVEMENT: MovementStrategy,
    TransactionTypeChoices.DELIVERY: DeliveryStrategy,
    TransactionTypeChoices.RECEIPT: ReceiptStrategy,
    TransactionTypeChoices.MACHINE_HOURS: MachineHoursStrategy,
}


class AssetTransactionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        user = request.user
        try:
            raw = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        serializer = AssetTransactionSerializer(data=raw)
        if not serializer.is_valid():
            return JsonResponse(serializer.errors, status=400)
        transaction_type = serializer.validated_data["transaction_type"]
        try:
            asset = Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)
        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        strategy_class = TRANSACTION_STRATEGY_MAP.get(transaction_type)
        if strategy_class is None:
            return JsonResponse({"error": "Unsupported transaction type."}, status=400)
        return strategy_class(raw, asset, employee, user).execute()


class DeliveryCheckView(APIView):
    """Zones the driver can pick from for a cuba action (recoger/dejar/verteo) at their
    current GPS position. Always returns the candidate list, even a single match — the
    driver confirms explicitly every time rather than having one silently auto-picked.

    For "dejar" specifically: the geo search only covers almacenes. The plan's own
    destination (an obra) is added on top, but only when the driver is actually near
    it — never other, unrelated obras. When it's too far, destination_too_far/
    destination_zone_name tell the client to say so instead of just omitting it."""
    permission_classes = [IsAuthenticated]

    def get(self, request, asset_id):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "Employee not found"}, status=403)
        try:
            asset = Asset.objects.select_related("current_working_zone").get(id=asset_id, company=employee.company)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)
        zones_data = []
        nearby_zones = []
        destination_too_far = False
        destination_zone_name = None
        lat_param = request.query_params.get("lat")
        lon_param = request.query_params.get("lon")
        action = request.query_params.get("action", "")
        planning_dest_zone_id = None  # id of the planning's destination zone, if included
        if lat_param is not None and lon_param is not None:
            try:
                latitude = float(lat_param)
                longitude = float(lon_param)
                if action == "verteo":
                    nearby_zones = find_zones_within_radius(
                        latitude, longitude, asset.company, zone_type=WorkingZone.ZoneType.VERTEDERO
                    )
                elif action == "dejar":
                    # Geo search only for almacenes; planning destination is added separately.
                    nearby_zones = find_zones_within_radius(
                        latitude, longitude, asset.company, zone_type=WorkingZone.ZoneType.ALMACEN
                    )
                    planning_id_param = request.query_params.get("planning_id")
                    if planning_id_param:
                        try:
                            planning = AssetPlanning.objects.select_related("destination").filter(
                                id=int(planning_id_param), company=asset.company, destination__isnull=False
                            ).first()
                            if planning and planning.destination:
                                dz = planning.destination
                                dz_lat, dz_lon = get_zone_coordinates(dz)
                                if dz_lat is not None:
                                    dist = haversine_distance(latitude, longitude, dz_lat, dz_lon)
                                    if dist <= MAX_ZONE_RADIUS_M:
                                        nearby_zones.append((dz, dist))
                                        planning_dest_zone_id = dz.id
                                    else:
                                        destination_too_far = True
                                        destination_zone_name = str(dz)
                        except (ValueError, TypeError):
                            pass
                else:
                    nearby_zones = find_zones_within_radius(
                        latitude, longitude, asset.company,
                        zone_types=[WorkingZone.ZoneType.OBRA, WorkingZone.ZoneType.ALMACEN]
                    )
                if len(nearby_zones) == 0 and action == "verteo":
                    return JsonResponse({"can_deliver": False, "verteo_required": False, "is_almacen": False, "zones": [], "not_near_vertedero": True, "at_vertedero": False})
                if len(nearby_zones) == 0 and action == "dejar":
                    vertedero_zones = find_zones_within_radius(latitude, longitude, asset.company, zone_type=WorkingZone.ZoneType.VERTEDERO)
                    if len(vertedero_zones) > 0:
                        return JsonResponse({"can_deliver": False, "verteo_required": False, "is_almacen": False, "zones": [], "not_near_vertedero": False, "at_vertedero": True})
                zones_data = [
                    {
                        "id": z.id,
                        "name": str(z),
                        "zone_type": z.zone_type,
                        "is_planning_destination": z.id == planning_dest_zone_id,
                    }
                    for z, _ in nearby_zones
                ]
            except (ValueError, TypeError):
                pass
        if lat_param is not None and lon_param is not None:
            is_almacen = len(nearby_zones) == 1 and nearby_zones[0][0].zone_type == WorkingZone.ZoneType.ALMACEN
        else:
            is_almacen = asset.current_working_zone is not None and asset.current_working_zone.zone_type == WorkingZone.ZoneType.ALMACEN
        verteo_required = asset.verteo_pending_transaction is not None
        can_deliver = not verteo_required or is_almacen
        response_data = {"verteo_required": verteo_required, "is_almacen": is_almacen, "can_deliver": can_deliver, "not_near_vertedero": False, "at_vertedero": False}
        if lat_param is not None and lon_param is not None:
            response_data["zones"] = zones_data
            response_data["destination_too_far"] = destination_too_far
            response_data["destination_zone_name"] = destination_zone_name
        return JsonResponse(response_data)


class AssetCheckDeliveryLocationView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        try:
            data = json.loads(request.body)
            latitude = data.get("latitude", 0)
            longitude = data.get("longitude", 0)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        try:
            asset = Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)
        destination_zone, _distance = find_zone_within_radius(latitude, longitude, asset.company)
        if destination_zone is None:
            return JsonResponse({"allowed": False, "error": "No estás cerca de ninguna zona de entrega. Acércate más."}, status=200)
        return JsonResponse({"allowed": True})


class MachineCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, asset_id):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        try:
            asset = Asset.objects.select_related("asset_type", "current_working_zone").get(id=asset_id, company=employee.company)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Maquina no encontrada."}, status=404)
        has_permission = MachinePermission.objects.filter(employee=employee, asset=asset).exists()
        if not has_permission:
            return JsonResponse({"error": "No tienes permiso para operar esta maquina."}, status=403)
        today = _today_str()
        open_tx = _open_machine_qs(asset, employee, today).first()
        completed_tx = _closed_machine_qs(asset, employee, today).order_by("-data__machine_start_time").first()
        if open_tx:
            intervals = open_tx.data.get("intervals", [])
            last = intervals[-1] if intervals else None
            estado = "working" if (last and last.get("end") is None) else "paused"
            tx = open_tx
        elif completed_tx:
            estado = "none"
            tx = completed_tx
        else:
            estado = "none"
            tx = None
        intervals = [{"start": iv.get("start"), "end": iv.get("end")} for iv in tx.data.get("intervals", [])] if tx else []
        return JsonResponse({
            "asset_id": asset.id,
            "asset_name": asset.name,
            "asset_type": asset.asset_type.name if asset.asset_type else "",
            "obra": str(asset.current_working_zone) if asset.current_working_zone else "",
            "planning_id": None,
            "estado": estado,
            "real_start_time": _format_time(open_tx.data.get("machine_start_time")) if open_tx else None,
            "real_end_time": None,
            "intervals": intervals if open_tx else [],
        })


class MachineHoursListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"machines": []})
        today = _today_str()
        machines = []
        seen_asset_ids = set()

        plannings = AssetPlanning.objects.select_related(
            "asset", "asset__asset_type", "asset__current_working_zone"
        ).filter(assigned_to=employee, planning_type=PlanningType.MACHINE_HOURS, scheduled_date=timezone.now().date(), status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT])

        for planning in plannings:
            asset = planning.asset
            seen_asset_ids.add(asset.id)
            open_tx = _open_machine_qs(asset, employee, today).filter(data__planning_id=planning.id).first()
            completed_tx = _closed_machine_qs(asset, employee, today).filter(data__planning_id=planning.id).first()
            if completed_tx:
                estado = "completed"
            elif open_tx:
                intervals = open_tx.data.get("intervals", [])
                last_interval = intervals[-1] if intervals else None
                estado = "working" if (last_interval and last_interval.get("end") is None) else "paused"
            else:
                estado = "none"
            ref_tx = open_tx or completed_tx
            machines.append({
                "planning_id": planning.id,
                "asset_id": asset.id,
                "asset_name": asset.name,
                "asset_type": asset.asset_type.name if asset.asset_type else "",
                "obra": str(asset.current_working_zone) if asset.current_working_zone else "",
                "estado": estado,
                "real_start_time": _format_time(ref_tx.data.get("machine_start_time")) if ref_tx else None,
                "real_end_time": _format_time(completed_tx.data.get("machine_end_time")) if completed_tx else None,
                "intervals": [{"start": iv.get("start"), "end": iv.get("end")} for iv in ref_tx.data.get("intervals", [])] if ref_tx else [],
            })

        qr_open_txs = list(
            _open_machine_qs(None, employee, today)
            .select_related("asset", "asset__asset_type", "asset__current_working_zone")
            .exclude(asset__isnull=True)
        )
        qr_open_txs = [tx for tx in qr_open_txs if tx.data.get('planning_id') is None]

        for tx in qr_open_txs:
            asset = tx.asset
            if asset.id in seen_asset_ids:
                continue
            seen_asset_ids.add(asset.id)
            intervals = tx.data.get("intervals", [])
            last_interval = intervals[-1] if intervals else None
            estado = "working" if (last_interval and last_interval.get("end") is None) else "paused"
            machines.append({
                "planning_id": None,
                "asset_id": asset.id,
                "asset_name": asset.name,
                "asset_type": asset.asset_type.name if asset.asset_type else "",
                "obra": str(asset.current_working_zone) if asset.current_working_zone else "",
                "estado": estado,
                "real_start_time": _format_time(tx.data.get("machine_start_time")),
                "real_end_time": None,
                "intervals": [{"start": iv.get("start"), "end": iv.get("end")} for iv in intervals],
            })

        return JsonResponse({"machines": machines})


class LaborerZonesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=403)
        try:
            latitude = float(request.query_params.get("latitude"))
            longitude = float(request.query_params.get("longitude"))
        except (TypeError, ValueError):
            return JsonResponse({"error": "latitude and longitude are required"}, status=400)
        authorized_zone_ids = WorkingZoneAuthorization.objects.filter(
            employee=employee, working_zone__company=employee.company, working_zone__is_active=True,
        ).values_list("working_zone_id", flat=True)
        zones = WorkingZone.objects.filter(pk__in=authorized_zone_ids)
        nearby = []
        for zone in zones:
            zone_lat, zone_lon = get_zone_coordinates(zone)
            if zone_lat is None:
                continue
            distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
            if distance <= MAX_ZONE_RADIUS_M:
                nearby.append({"id": zone.pk, "name": zone.name, "distance_m": round(distance)})
        if not nearby:
            return JsonResponse({"error": "No estás cerca de ninguna obra en la que estés autorizado a trabajar."}, status=403)
        return JsonResponse({"zones": nearby})


class LaborerHoursView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        strategy = LaborerHoursStrategy(data, employee, request.user)
        return strategy.execute()


class LaborerHoursStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=403)
        today = _today_str()
        open_tx = _open_laborer_qs(employee, today).select_related("to_zone").first()
        if not open_tx:
            return JsonResponse({"active": False})
        intervals = open_tx.data.get("intervals", [])
        last = intervals[-1] if intervals else None
        current_status = "started" if (last and last.get("end") is None) else "paused"
        return JsonResponse({
            "active": True,
            "transaction_id": open_tx.pk,
            "status": current_status,
            "zone_id": open_tx.to_zone_id,
            "zone_name": open_tx.to_zone.name if open_tx.to_zone else None,
            "intervals": intervals,
        })