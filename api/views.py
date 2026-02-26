from django.contrib.auth import authenticate
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.contrib.auth.models import User
from assets.models import Asset, AssetTransaction, TransactionTypeChoices
from company.models import WorkingZone
from django.utils import timezone
import json
import secrets
import math


# Simple in-memory token store (replace with database tokens in production)
active_tokens = {}


def get_user_from_token(request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Token "):
        return None
    token = auth_header.split(" ")[1]
    user_id = active_tokens.get(token)
    if not user_id:
        return None
    try:
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        return None


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in metres between two GPS coordinates."""
    R = 6371000  # Earth radius in metres
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_zone_coordinates(zone):
    """Extract latitude and longitude from a WorkingZone location field.
    The location field stores as 'SRID=4326;POINT (-3.674 40.432)' string."""
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


def find_zone_within_radius(latitude, longitude, company, radius_metres=10000):
    """Find a WorkingZone within radius_metres of the given coordinates."""
    zones = WorkingZone.objects.filter(company=company, is_active=True)
    for zone in zones:
        zone_lat, zone_lon = get_zone_coordinates(zone)
        if zone_lat is None:
            continue
        distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
        if distance <= radius_metres:
            return zone, distance
    return None, None


@csrf_exempt
@require_http_methods(["POST"])
def login_view(request):
    try:
        data = json.loads(request.body)
        username = data.get("username", "")
        password = data.get("password", "")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    user = authenticate(username=username, password=password)
    if user is None:
        return JsonResponse({"error": "Usuario o contraseña incorrectos"}, status=401)

    token = secrets.token_hex(32)
    active_tokens[token] = user.id

    return JsonResponse({
        "token": token,
        "user_id": user.id,
        "username": user.username,
        "full_name": f"{user.first_name} {user.last_name}".strip() or user.username,
    })


@csrf_exempt
@require_http_methods(["GET"])
def assets_view(request):
    user = get_user_from_token(request)
    if user is None:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    assets = Asset.objects.select_related("asset_type", "current_working_zone").filter(assigned_to=user)

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


@csrf_exempt
@require_http_methods(["POST"])
def pickup_view(request, asset_id):
    user = get_user_from_token(request)
    if user is None:
        return JsonResponse({"error": "Unauthorized"}, status=401)

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

    # DEBUG
    print(f"Asset: {asset.name}")
    print(f"Current working zone: {asset.current_working_zone}")
    print(f"Zone location: {asset.current_working_zone.location if asset.current_working_zone else 'None'}")
    print(f"Worker location: lat={latitude}, lon={longitude}")

    if asset.current_working_zone is None:
        return JsonResponse({"error": "This asset has no assigned pickup location."}, status=400)

    zone_lat, zone_lon = get_zone_coordinates(asset.current_working_zone)
    if zone_lat is None:
        return JsonResponse({"error": "Pickup zone has no coordinates configured."}, status=400)

    distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
    print(f"Zone coords: lat={zone_lat}, lon={zone_lon}")
    print(f"Distance: {distance}m")

    if distance > 10000:
        return JsonResponse({
            "error": f"Estas muy lejos de la ubicación ({asset.current_working_zone}). "
                     f"Estas a {int(distance)}m. Tienes que estar dentro de 50m de la ubicación."
        }, status=400)

    AssetTransaction.objects.create(
        asset=asset,
        company=asset.company,
        from_zone=asset.current_working_zone,
        to_zone=None,
        transaction_type=TransactionTypeChoices.MOVEMENT,
        date=timezone.now(),
        notes=f"Picked up by {user.username}. GPS: {latitude}, {longitude}. Distance from zone: {int(distance)}m",
    )

    return JsonResponse({"success": True, "message": "Pickup recorded"})


@csrf_exempt
@require_http_methods(["POST"])
def deliver_view(request, asset_id):
    user = get_user_from_token(request)
    if user is None:
        return JsonResponse({"error": "Unauthorized"}, status=401)

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

    # Find which zone the worker is currently at
    destination_zone, distance = find_zone_within_radius(latitude, longitude, asset.company)
    if destination_zone is None:
        return JsonResponse({
            "error": "No esta lo suficientemente cerca de ninguna ubicación conocida. Debes estar a menos de 50m de una ubicación para entregar."
        }, status=400)

    AssetTransaction.objects.create(
        asset=asset,
        company=asset.company,
        from_zone=None,
        to_zone=destination_zone,
        transaction_type=TransactionTypeChoices.DELIVERY,
        date=timezone.now(),
        notes=f"Delivered by {user.username} to {destination_zone}. GPS: {latitude}, {longitude}. Distance from zone: {int(distance)}m",
    )

    # Update the asset's current working zone to where it was delivered
    asset.current_working_zone = destination_zone
    asset.save()

    return JsonResponse({
        "success": True,
        "message": f"Delivery recorded to {destination_zone}"
    })


def get_asset_status(last_transaction):
    if last_transaction is None:
        return "AVAILABLE"
    if last_transaction.transaction_type == TransactionTypeChoices.MOVEMENT:
        return "PICKED_UP"
    if last_transaction.transaction_type == TransactionTypeChoices.DELIVERY:
        return "DELIVERED"
    return "AVAILABLE"