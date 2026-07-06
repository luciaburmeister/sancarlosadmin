import base64
import io
import json
import uuid

import qrcode
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
    # RECEIPT does not change the cuba's status — it's still PICKED_UP
    if last_transaction.transaction_type == TransactionTypeChoices.RECEIPT:
        # Walk back to the previous non-receipt transaction to get the real status
    if last_transaction.transaction_type == TransactionTypeChoices.RECEIPT:
        prev = AssetTransaction.objects.filter(
            asset=last_transaction.asset,
            date__lt=last_transaction.date,
        ).exclude(transaction_type=TransactionTypeChoices.RECEIPT).order_by("-date").first()
        return get_asset_status(prev)
    return "AVAILABLE"


def get_asset_receipt(asset):
    """
    True if the cuba has already been emptied since its most
    recent recogida. Used by the app to grey out the Verteo button so
    workers can't register it twice for the same pickup.
    """
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
        "asset_receipt": get_asset_receipt(asset),
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


class AssetTransactionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        user = request.user
        try:
            raw = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        transaction_type = raw.get("transaction_type", "").strip().lower()
        if transaction_type not in ("movement", "delivery", "receipt"):
            return JsonResponse({"error": "transaction_type must be movement, delivery, or receipt."}, status=400)

        try:
            asset = Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)

        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)

        if transaction_type == "movement":
            return self._strategy_movement(raw, asset, employee, user)
        elif transaction_type == "delivery":
            return self._strategy_delivery(raw, asset, employee, user)
        elif transaction_type == "receipt":
            return self._strategy_receipt(raw, asset, employee, user)

    def _strategy_movement(self, raw, asset, employee, user):
        latitude = raw.get("latitude", 0)
        longitude = raw.get("longitude", 0)
        firma = raw.get("firma", "")
        imagen = raw.get("imagen", "")
        form_data = {k: v for k, v in raw.items() if k not in (
            "transaction_type", "latitude", "longitude", "firma", "imagen"
        )}
        distance = None
        chosen_zone_id = data.get("zone_id")

        if chosen_zone_id:
            from company.models import WorkingZone
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

    def _strategy_delivery(self, raw, asset, employee, user):
        latitude = raw.get("latitude", 0)
        longitude = raw.get("longitude", 0)
        firma = raw.get("firma", "")
        imagen = raw.get("imagen", "")
        form_data = {k: v for k, v in raw.items() if k not in (
            "transaction_type", "latitude", "longitude", "firma", "imagen"
        )}
        destination_zone, distance = find_zone_within_radius(latitude, longitude, asset.company)
        if destination_zone is None:
            return JsonResponse({"error": "No estas cerca de ninguna zona de entrega conocida."}, status=400)
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
            firma=firma,
            data=form_data,
        )
        image_file = _base64_to_image_file(imagen)
        if image_file:
            transaction.imagen.save(image_file.name, image_file, save=False)
        transaction.save()
        asset.current_working_zone = destination_zone
        asset.save()
        send_asset_transaction_email_task.delay(transaction.id)
        return JsonResponse({"success": True, "message": f"Entrega registrada en {destination_zone}", "transaction_id": transaction.id})

    def _strategy_receipt(self, raw, asset, employee, user):
        nro_albaran = raw.get("nro_albaran", "").strip()
        foto_b64 = raw.get("foto_albaran", "")
        if not nro_albaran:
            return JsonResponse({"error": "El numero de albaran es obligatorio."}, status=400)
        transaction = AssetTransaction(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=None,
            transaction_type=TransactionTypeChoices.RECEIPT,
            date=timezone.now(),
            performed_by=employee,
            notes=f"Verteo registrado por {user.username}. Albaran: {nro_albaran}.",
        )
        transaction.save()
        receipt = AssetTransactionReceipt(
            transaction=transaction,
            nro_albaran=nro_albaran,
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