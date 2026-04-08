from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_http_methods
from assets.models import Asset, AssetTransaction, TransactionTypeChoices
from company.models import WorkingZone
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.response import Response
import json
import math
import qrcode
import io
from company.models import WorkAttendance, Employee
from decimal import Decimal


# ✅ Custom login view that returns access + refresh + user info
class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        user = self.user
        data["user_id"] = user.id
        data["username"] = user.username
        data["full_name"] = f"{user.first_name} {user.last_name}".strip() or user.username
        return data

class login_view(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_zone_coordinates(zone):
    try:
        point_str = str(zone.location)
        if ";" in point_str:
            point_str = point_str.split(";")[1]
        coords = point_str.replace("POINT (", "").replace("POINT(", "").replace(")", "").split()
        lon = float(coords[0])
        lat = float(coords[1])
        return lat, lon
    except Exception:
        return None, None


def find_zone_within_radius(latitude, longitude, company, radius_metres=100):
    zones = WorkingZone.objects.filter(company=company, is_active=True)
    for zone in zones:
        zone_lat, zone_lon = get_zone_coordinates(zone)
        if zone_lat is None:
            continue
        distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
        if distance <= radius_metres:
            return zone, distance
    return None, None


def get_asset_status(last_transaction):
    if last_transaction is None:
        return "AVAILABLE"
    if last_transaction.transaction_type == TransactionTypeChoices.MOVEMENT:
        return "PICKED_UP"
    if last_transaction.transaction_type == TransactionTypeChoices.DELIVERY:
        return "DELIVERED"
    return "AVAILABLE"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def assets_view(request):
    user = request.user

    try:
        employee = user.employee_profile
        assets = Asset.objects.select_related("asset_type", "current_working_zone").filter(assigned_to=employee)
    except Exception:
        return JsonResponse({"assets": []})
   
    data = []
    for asset in assets:
        last_tx = asset.transactions.order_by("-date").first()
        zone = asset.current_working_zone
        zone_coords = get_zone_coordinates(zone) if zone else (None, None)
        data.append({
            "id": asset.id,
            "code": asset.name,
            "description": asset.asset_type.name if asset.asset_type else "",
            "status": get_asset_status(last_tx),
            "last_location": str(zone) if zone else None,
            "last_updated": last_tx.date.strftime("%d/%m/%Y %H:%M") if last_tx else None,
            "zone_address": f"{zone.street_address}, {zone.city}, {zone.postal_code}" if zone else None,
            "zone_lat": zone_coords[0],
            "zone_lon": zone_coords[1],
        })

    return JsonResponse({"assets": data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def asset_by_id_view(request, asset_id):
    user = request.user

    try:
        asset = Asset.objects.select_related("asset_type", "current_working_zone").get(id=asset_id)
    except Asset.DoesNotExist:
        return JsonResponse({"error": "Asset not found"}, status=404)

    if asset.company != user.employee_profile.company:
        return JsonResponse({"error": "Asset not found"}, status=404)

    last_tx = asset.transactions.order_by("-date").first()
    zone = asset.current_working_zone
    zone_coords = get_zone_coordinates(zone) if zone else (None, None)

    return JsonResponse({
        "id": asset.id,
        "code": asset.name,
        "description": asset.asset_type.name if asset.asset_type else "",
        "status": get_asset_status(last_tx),
        "last_location": str(zone) if zone else None,
        "last_updated": last_tx.date.strftime("%d/%m/%Y %H:%M") if last_tx else None,
        "zone_address": f"{zone.street_address}, {zone.city}, {zone.postal_code}" if zone else None,
        "zone_lat": zone_coords[0],
        "zone_lon": zone_coords[1],
        "assigned_to": asset.assigned_to.user.username if asset.assigned_to and asset.assigned_to.user else None,
        "assigned_to_me": asset.assigned_to == user.employee_profile,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def assign_view(request, asset_id):
    user = request.user

    try:
        asset = Asset.objects.get(id=asset_id)
    except Asset.DoesNotExist:
        return JsonResponse({"error": "Asset not found"}, status=404)

    try:
        employee = user.employee_profile
    except Exception:
        return JsonResponse({"error": "No employee profile found"}, status=400)

    if asset.company != employee.company:
        return JsonResponse({"error": "Este cubo no pertenece a tu empresa"}, status=403)

    asset.assigned_to = employee
    asset.save()

    return JsonResponse({"success": True, "message": f"Cubo {asset.name} asignado a {employee.full_name}"})

@require_http_methods(["GET"])
def asset_qr_view(request, asset_id):
    try:
        asset = Asset.objects.get(id=asset_id)
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def pickup_view(request, asset_id):
    user = request.user

    try:
        data = json.loads(request.body)
        latitude = data.get("latitude", 0)
        longitude = data.get("longitude", 0)
        firma = data.get("firma", "")
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

    if distance > 100:
        return JsonResponse({
            "error": f"Estás demasiado lejos de {asset.current_working_zone}. "
                     f"Estás a {int(distance)}m. Debes estar a menos de 100m."
        }, status=400)

    AssetTransaction.objects.create(
        asset=asset,
        company=asset.company,
        from_zone=asset.current_working_zone,
        to_zone=None,
        transaction_type=TransactionTypeChoices.MOVEMENT,
        date=timezone.now(),
        notes=f"Recogido por {user.username}. GPS: {latitude}, {longitude}. Distancia: {int(distance)}m",
        firma = firma,
    )

    return JsonResponse({"success": True, "message": "Recogida registrada"})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def deliver_view(request, asset_id):
    user = request.user

    try:
        data = json.loads(request.body)
        latitude = data.get("latitude", 0)
        longitude = data.get("longitude", 0)
        firma = data.get("firma", "")  
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        asset = Asset.objects.get(id=asset_id)
    except Asset.DoesNotExist:
        return JsonResponse({"error": "Asset not found"}, status=404)

    destination_zone, distance = find_zone_within_radius(latitude, longitude, asset.company)
    if destination_zone is None:
        return JsonResponse({
            "error": "No estás cerca de ninguna zona de entrega conocida. Debes estar a menos de 10m."
        }, status=400)

    AssetTransaction.objects.create(
        asset=asset,
        company=asset.company,
        from_zone=None,
        to_zone=destination_zone,
        transaction_type=TransactionTypeChoices.DELIVERY,
        date=timezone.now(),
        notes=f"Entregado por {user.username} en {destination_zone}. GPS: {latitude}, {longitude}. Distancia: {int(distance)}m",
        firma = firma,
    )

    asset.current_working_zone = destination_zone
    asset.save()

    return JsonResponse({
        "success": True,
        "message": f"Entrega registrada en {destination_zone}"
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def clock_in_view(request):
    user = request.user

    try:
        data = json.loads(request.body)
        latitude = data.get("latitude")
        longitude = data.get("longitude")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        employee = user.employee_profile
    except Exception:
        return JsonResponse({"error": "No employee profile found for this user"}, status=400)

    # Check if already clocked in today without clocking out
    existing = WorkAttendance.objects.filter(
        employee=employee,
        clock_out_time__isnull=True
    ).first()

    if existing:
        return JsonResponse({"error": "Ya has fichado entrada. Ficha salida primero."}, status=400)

    attendance = WorkAttendance.objects.create(
        employee=employee,
        company=employee.company,
        clock_in_time=timezone.now(),
        clock_in_lat=Decimal(str(latitude)) if latitude else None,
        clock_in_lng=Decimal(str(longitude)) if longitude else None,
    )

    return JsonResponse({
        "success": True,
        "message": "Entrada registrada",
        "attendance_id": attendance.id,
        "clock_in_time": attendance.clock_in_time.strftime("%d/%m/%Y %H:%M:%S"),
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def clock_out_view(request):
    user = request.user

    try:
        data = json.loads(request.body)
        latitude = data.get("latitude")
        longitude = data.get("longitude")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    try:
        employee = user.employee_profile
    except Exception:
        return JsonResponse({"error": "No employee profile found for this user"}, status=400)

    # Find the open attendance record
    attendance = WorkAttendance.objects.filter(
        employee=employee,
        clock_out_time__isnull=True
    ).first()

    if not attendance:
        return JsonResponse({"error": "No hay fichaje de entrada activo."}, status=400)

    attendance.clock_out_time = timezone.now()
    attendance.clock_out_lat = Decimal(str(latitude)) if latitude else None
    attendance.clock_out_lng = Decimal(str(longitude)) if longitude else None
    attendance.save()

    return JsonResponse({
        "success": True,
        "message": "Salida registrada",
        "clock_out_time": attendance.clock_out_time.strftime("%d/%m/%Y %H:%M:%S"),
    })

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def attendance_status_view(request):
    user = request.user
    try:
        employee = user.employee_profile
    except Exception:
        return JsonResponse({"is_clocked_in": False})

    is_clocked_in = WorkAttendance.objects.filter(
        employee=employee,
        clock_out_time__isnull=True
    ).exists()

    return JsonResponse({"is_clocked_in": is_clocked_in})
