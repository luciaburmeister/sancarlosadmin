import io
import json

import qrcode
from assets.models import Asset, AssetTransaction, TransactionTypeChoices
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from api.constants import MAX_ZONE_RADIUS_M
from api.fleet.utils import find_zone_within_radius, get_zone_coordinates, haversine_distance
from company.utils import get_current_employee


def get_asset_status(last_transaction):
    if last_transaction is None:
        return "AVAILABLE"
    if last_transaction.transaction_type == TransactionTypeChoices.MOVEMENT:
        return "PICKED_UP"
    if last_transaction.transaction_type == TransactionTypeChoices.DELIVERY:
        return "DELIVERED"
    return "AVAILABLE"


def _serialize_asset_list_item(asset, last_tx, zone, zone_coords):
    return {
        "id": asset.id,
        "code": asset.name,
        "description": asset.asset_type.name if asset.asset_type else "",
        "status": get_asset_status(last_tx),
        "last_location": str(zone) if zone else None,
        "last_updated": last_tx.date.strftime("%d/%m/%Y %H:%M") if last_tx else None,
        "zone_address": f"{zone.street_address}, {zone.city}, {zone.postal_code}" if zone else None,
        "zone_lat": zone_coords[0],
        "zone_lon": zone_coords[1],
        "cliente": asset.client.name if asset.client else "",
        "obra": str(zone) if zone else "",
    }


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


class AssetPickupView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        user = request.user
        try:
            raw = json.loads(request.body)
            latitude = raw.get("latitude", 0)
            longitude = raw.get("longitude", 0)
            firma = raw.get("firma", "")
            form_data = {k: v for k, v in raw.items() if k not in ("latitude", "longitude", "firma")}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            asset = Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)

        if asset.current_working_zone is None:
            return JsonResponse({"error": "Este cubo no tiene ubicación de recogida asignada."}, status=400)

        zone_lat, zone_lon = get_zone_coordinates(asset.current_working_zone)
        if zone_lat is None:
            return JsonResponse({"error": "La zona de recogida no tiene coordenadas configuradas."}, status=400)

        distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
        print(
            f"PICKUP: your GPS={latitude},{longitude}, zone={zone_lat},{zone_lon}, "
            f"distance={int(distance)}m, max={MAX_ZONE_RADIUS_M}m"
        )

        if distance > MAX_ZONE_RADIUS_M:
            return JsonResponse({
                "error": f"Estás demasiado lejos de {asset.current_working_zone}. "
                         f"Estás a {int(distance)}m. Debes estar a menos de {MAX_ZONE_RADIUS_M}m."
            }, status=400)

        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)

        AssetTransaction.objects.create(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=None,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            performed_by=employee,
            notes=f"Recogido por {user.username}. GPS: {latitude}, {longitude}. Distancia: {int(distance)}m",
            firma=firma,
            data=form_data,
        )

        return JsonResponse({"success": True, "message": "Recogida registrada"})


class AssetDeliverView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        user = request.user
        try:
            raw = json.loads(request.body)
            latitude = raw.get("latitude", 0)
            longitude = raw.get("longitude", 0)
            firma = raw.get("firma", "")
            form_data = {k: v for k, v in raw.items() if k not in ("latitude", "longitude", "firma")}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            asset = Asset.objects.get(id=asset_id)
        except Asset.DoesNotExist:
            return JsonResponse({"error": "Asset not found"}, status=404)

        destination_zone, distance = find_zone_within_radius(latitude, longitude, asset.company)
        if destination_zone is None:
            return JsonResponse({"error": "No estás cerca de ninguna zona de entrega conocida."}, status=400)

        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)

        AssetTransaction.objects.create(
            asset=asset,
            company=asset.company,
            from_zone=asset.current_working_zone,
            to_zone=destination_zone,
            transaction_type=TransactionTypeChoices.DELIVERY,
            date=timezone.now(),
            performed_by=employee,
            project_file=destination_zone.project_file if destination_zone else None,
            notes=(
                f"Entregado por {user.username} en {destination_zone}. "
                f"GPS: {latitude}, {longitude}. Distancia: {int(distance)}m"
            ),
            firma=firma,
            data=form_data,
        )

        asset.current_working_zone = destination_zone
        asset.save()

        return JsonResponse({"success": True, "message": f"Entrega registrada en {destination_zone}"})


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
