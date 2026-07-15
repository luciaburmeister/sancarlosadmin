import base64
import io
import json
import uuid
import logging

import qrcode
from company.models import WorkingZone
from django.core.files.base import ContentFile
from assets.models import Asset, AssetTransaction, TransactionTypeChoices, AssetTransactionReceipt
from core.tasks import send_asset_transaction_email_task
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from api.constants import MAX_ZONE_RADIUS_M
from api.fleet.serializers import AssetTransactionSerializer
from api.fleet.utils import find_zone_within_radius, get_zone_coordinates, haversine_distance, find_zones_within_radius
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


def get_asset_status(last_transaction):
    if last_transaction is None:
        return "AVAILABLE"
    if last_transaction.transaction_type == TransactionTypeChoices.MOVEMENT:
        return "PICKED_UP"
    if last_transaction.transaction_type == TransactionTypeChoices.DELIVERY:
        return "DELIVERED"
    if last_transaction.transaction_type == TransactionTypeChoices.RECEIPT:
        prev = AssetTransaction.objects.filter(
            asset=last_transaction.asset,
            date__lt=last_transaction.date,
        ).exclude(transaction_type=TransactionTypeChoices.RECEIPT).order_by("-date").first()
        return get_asset_status(prev)
    return "AVAILABLE"


def get_asset_receipt(asset):
    last_movement = (
        AssetTransaction.objects.filter(
            asset=asset, transaction_type=TransactionTypeChoices.MOVEMENT
        )
        .order_by("-date")
        .first()
    )
    if last_movement is None:
        return False

    last_receipt = (
        AssetTransaction.objects.filter(
            asset=asset, transaction_type=TransactionTypeChoices.RECEIPT
        )
        .order_by("-date")
        .first()
    )
    if last_receipt is None:
        return False

    return last_receipt.date > last_movement.date


def _serialize_asset_list_item(asset, last_tx, zone, zone_coords):
    return {
        "id": asset.id,
        "code": asset.name,
        "description": asset.asset_type.name if asset.asset_type else "",
        "status": get_asset_status(last_tx),
        "asset_receipt": asset.has_pending_receipt,
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
        assets = Asset.objects.select_related("asset_type", "current_working_zone").filter(
            assigned_to=employee
        )
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
        asset.assigned_to = employee
        asset.save()
        return JsonResponse({"success": True, "message": f"Cubo {asset.name} asignado a {employee.full_name}"})


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

    def execute(self) -> JsonResponse:
        raise NotImplementedError


class MovementStrategy(BaseTransactionStrategy):
    def execute(self) -> JsonResponse:
        data = self.data
        asset = self.asset
        employee = self.employee
        user = self.user

        latitude = data.get("latitude", 0)
        longitude = data.get("longitude", 0)
        form_data = {k: v for k, v in data.items() if k not in (
            "transaction_type", "latitude", "longitude", "firma", "imagen", "zone_id"
        )}
        distance = None
        chosen_zone_id = data.get("zone_id")

        if chosen_zone_id:
            try:
                detected_zone = WorkingZone.objects.get(id=chosen_zone_id, company=asset.company)
                asset.current_working_zone = detected_zone
                asset.save(update_fields=["current_working_zone"])
            except WorkingZone.DoesNotExist:
                return JsonResponse({"error": "Zona no encontrada."}, status=400)

        elif asset.current_working_zone is not None:
            zone_lat, zone_lon = get_zone_coordinates(asset.current_working_zone)
            if zone_lat is not None:
                distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
                if distance > MAX_ZONE_RADIUS_M:
                    return JsonResponse({
                        "error": f"Estas demasiado lejos de {asset.current_working_zone}. "
                                 f"Estas a {int(distance)}m. Debes estar a menos de {MAX_ZONE_RADIUS_M}m."
                    }, status=400)

        else:
            nearby_zones = find_zones_within_radius(latitude, longitude, asset.company)
            if len(nearby_zones) == 0:
                return JsonResponse({"error": "No estas cerca de ninguna zona conocida."}, status=400)
            elif len(nearby_zones) > 1:
                return JsonResponse({
                    "multiple_zones": True,
                    "zones": [{"id": z.id, "name": str(z)} for z, d in nearby_zones]
                }, status=200)
            else:
                detected_zone, distance = nearby_zones[0]
                asset.current_working_zone = detected_zone
                asset.save(update_fields=["current_working_zone"])

        distance_note = f"Distancia: {int(distance)}m" if distance is not None else "Sin zona asignada"
        transaction = AssetTransaction(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=None,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            performed_by=employee,
            project_file=asset.current_working_zone.project_file if asset.current_working_zone else None,
            notes=f"Recogido por {user.username}. GPS: {latitude}, {longitude}. {distance_note}",
            data=form_data,
        )
        self._save_firma_and_imagen(transaction)
        transaction.save()
        send_asset_transaction_email_task.delay(transaction.id)
        return JsonResponse({"success": True, "message": "Recogida registrada", "transaction_id": transaction.id})


class DeliveryStrategy(BaseTransactionStrategy):
    def execute(self) -> JsonResponse:
        data = self.data
        asset = self.asset
        employee = self.employee
        user = self.user

        latitude = data.get("latitude", 0)
        longitude = data.get("longitude", 0)
        form_data = {k: v for k, v in data.items() if k not in (
            "transaction_type", "latitude", "longitude", "firma", "imagen", "zone_id"
        )}

        chosen_zone_id = data.get("zone_id")

        if chosen_zone_id:
            try:
                destination_zone = WorkingZone.objects.get(id=chosen_zone_id, company=asset.company)
                distance = 0
            except WorkingZone.DoesNotExist:
                return JsonResponse({"error": "Zona no encontrada."}, status=400)
        else:
            nearby_zones = find_zones_within_radius(latitude, longitude, asset.company)
            if len(nearby_zones) == 0:
                return JsonResponse({"error": "No estas cerca de ninguna zona de entrega conocida."}, status=400)
            elif len(nearby_zones) > 1:
                return JsonResponse({
                    "multiple_zones": True,
                    "zones": [{"id": z.id, "name": str(z)} for z, d in nearby_zones]
                }, status=200)
            else:
                destination_zone, distance = nearby_zones[0]

        transaction = AssetTransaction(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=destination_zone,
            transaction_type=TransactionTypeChoices.DELIVERY,
            date=timezone.now(),
            performed_by=employee,
            project_file=destination_zone.project_file if destination_zone else None,
            notes=f"Entregado por {user.username} en {destination_zone}. GPS: {latitude}, {longitude}. Distancia: {int(distance)}m",
            data=form_data,
        )
        self._save_firma_and_imagen(transaction)
        transaction.save()
        asset.current_working_zone = destination_zone
        asset.save()
        send_asset_transaction_email_task.delay(transaction.id)
        return JsonResponse({"success": True, "message": f"Entrega registrada en {destination_zone}", "transaction_id": transaction.id})


class ReceiptStrategy(BaseTransactionStrategy):
    def execute(self) -> JsonResponse:
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
            nearby_zones = find_zones_within_radius(
                latitude, longitude, asset.company,
                zone_type=WorkingZone.ZoneType.VERTEDERO
            )
            if len(nearby_zones) > 1:
                return JsonResponse({
                    "multiple_zones": True,
                    "zones": [{"id": z.id, "name": str(z)} for z, d in nearby_zones]
                }, status=200)
            elif len(nearby_zones) == 1:
                vertedero_zone, _ = nearby_zones[0]

        # Link verteo to the original obra pickup, not the almacen pickup
        movement_tx = (
            AssetTransaction.objects.filter(
                asset=asset,
                transaction_type=TransactionTypeChoices.MOVEMENT,
                from_zone__zone_type=WorkingZone.ZoneType.OBRA,
            )
            .order_by("-date")
            .first()
        )

        form_data = {k: v for k, v in data.items() if k not in (
            "transaction_type", "latitude", "longitude", "zone_id", "foto_albaran",
        )}

        transaction = AssetTransaction(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=vertedero_zone,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
            performed_by=employee,
            notes=f"Verteo registrado por {user.username}. Albaran: {nro_albaran}. Material: {material_vertido}. Peso: {peso_neto}kg.",
            data=form_data,
        )
        transaction.save()

        receipt = AssetTransactionReceipt(
            transaction=transaction,
            nro_albaran=nro_albaran,
            data={
                "peso_neto": peso_neto,
                "material_vertido": material_vertido,
            },
            movement_transaction=movement_tx,
        )
        foto_file = _base64_to_image_file(foto_b64)
        if foto_file:
            receipt.foto.save(foto_file.name, foto_file, save=False)
        receipt.save()

        return JsonResponse({
            "success": True,
            "message": "Verteo registrado correctamente.",
            "transaction_id": transaction.id,
            "receipt_id": receipt.id,
        })


TRANSACTION_STRATEGY_MAP = {
    TransactionTypeChoices.MOVEMENT: MovementStrategy,
    TransactionTypeChoices.DELIVERY: DeliveryStrategy,
    TransactionTypeChoices.RECEIPT: ReceiptStrategy,
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
    """
    GET /en/api/assets/<asset_id>/delivery-check/

    Called by the app before the driver opens any form (Recoger, Dejar, Verteo).

    Optional query params:
      ?lat=<float>&lon=<float>&action=<verteo|recoger|dejar>

    - verteo: only searches vertedero zones; blocks if none found
    - recoger/dejar: excludes vertedero zones; if zero found, checks if
      driver is at a vertedero and blocks dejar with at_vertedero=True
    - verteo_required is checked against the last obra pickup only,
      so almacen intermediate stops don't reset the verteo requirement
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, asset_id):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "Employee not found"}, status=403)

        try:
            asset = Asset.objects.select_related("current_working_zone").get(
                id=asset_id,
                company=employee.company
            )
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)

        multiple_zones = False
        zones_data = []
        detected_zone_id = None
        nearby_zones = []

        lat_param = request.query_params.get("lat")
        lon_param = request.query_params.get("lon")
        action = request.query_params.get("action", "")

        if lat_param is not None and lon_param is not None:
            try:
                latitude = float(lat_param)
                longitude = float(lon_param)

                if action == "verteo":
                    zone_type_filter = WorkingZone.ZoneType.VERTEDERO
                elif action in ("recoger", "dejar"):
                    zone_type_filter = None
                else:
                    zone_type_filter = None

                nearby_zones = find_zones_within_radius(
                    latitude, longitude, asset.company,
                    zone_type=zone_type_filter,
                    zone_types=[WorkingZone.ZoneType.OBRA, WorkingZone.ZoneType.ALMACEN] if action in ("recoger", "dejar") else None
                )

                if len(nearby_zones) > 1:
                    multiple_zones = True
                    zones_data = [
                        {"id": z.id, "name": str(z), "zone_type": z.zone_type}
                        for z, _ in nearby_zones
                    ]
                elif len(nearby_zones) == 1:
                    detected_zone_id = nearby_zones[0][0].id
                elif len(nearby_zones) == 0 and action == "verteo":
                    return JsonResponse({
                        "can_deliver": False,
                        "verteo_required": False,
                        "is_almacen": False,
                        "multiple_zones": False,
                        "not_near_vertedero": True,
                        "at_vertedero": False,
                    })
                elif len(nearby_zones) == 0 and action == "dejar":
                    # No obra/almacen nearby — check if driver is at a vertedero
                    vertedero_zones = find_zones_within_radius(
                        latitude, longitude, asset.company,
                        zone_type=WorkingZone.ZoneType.VERTEDERO
                    )
                    if len(vertedero_zones) > 0:
                        return JsonResponse({
                            "can_deliver": False,
                            "verteo_required": False,
                            "is_almacen": False,
                            "multiple_zones": False,
                            "not_near_vertedero": False,
                            "at_vertedero": True,
                        })

            except (ValueError, TypeError):
                pass

        # is_almacen based on destination zone only when GPS provided
        if lat_param is not None and lon_param is not None:
            if len(nearby_zones) == 1:
                is_almacen = nearby_zones[0][0].zone_type == WorkingZone.ZoneType.ALMACEN
            else:
                is_almacen = False
        else:
            is_almacen = (
                asset.current_working_zone is not None
                and asset.current_working_zone.zone_type == WorkingZone.ZoneType.ALMACEN
            )

        # Check verteo requirement against last obra pickup only —
        # almacen intermediate pickups don't reset the verteo requirement
        last_movement = (
            AssetTransaction.objects.filter(
                asset=asset,
                transaction_type=TransactionTypeChoices.MOVEMENT,
                from_zone__zone_type=WorkingZone.ZoneType.OBRA,
            )
            .order_by("-date")
            .first()
        )

        verteo_required = False
        if last_movement is not None:
            vertedero_value = last_movement.data.get("vertedero", "")
            verteo_required = bool(vertedero_value and vertedero_value.strip())

        if verteo_required and last_movement is not None:
            already_done = AssetTransaction.objects.filter(
                asset=asset,
                transaction_type=TransactionTypeChoices.RECEIPT,
                date__gt=last_movement.date,
            ).exists()
            if already_done:
                verteo_required = False

        can_deliver = not verteo_required or is_almacen

        response_data = {
            "verteo_required": verteo_required,
            "is_almacen": is_almacen,
            "can_deliver": can_deliver,
            "not_near_vertedero": False,
            "at_vertedero": False,
        }

        if lat_param is not None and lon_param is not None:
            response_data["multiple_zones"] = multiple_zones
            response_data["detected_zone_id"] = detected_zone_id
            if multiple_zones:
                response_data["zones"] = zones_data

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
            return JsonResponse({
                "allowed": False,
                "error": "No estás cerca de ninguna zona de entrega. Acércate más.",
            }, status=200)

        return JsonResponse({"allowed": True})