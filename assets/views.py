from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
import math

from .models import Asset, AssetTransaction, TransactionTypeChoices
from .serializers import AssetTransactionSerializer


def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in metres between two GPS coordinates."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_zone_coordinates(zone):
    """Extract lat/lon from the WorkingZone location field (POINT format)."""
    try:
        point_str = str(zone.location)  # e.g. "SRID=4326;POINT (-3.68 40.40)"
        coords = point_str.split("POINT (")[1].replace(")", "").split()
        lon = float(coords[0])
        lat = float(coords[1])
        return lat, lon
    except Exception:
        return None, None


class DeliverAssetView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, asset_id):
        firma = request.data.get('firma', '')
        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')

        if latitude is None or longitude is None:
            return Response({'error': 'latitude and longitude are required'}, status=status.HTTP_400_BAD_REQUEST)

        employee = request.user.employee_profile

        try:
            asset = Asset.objects.get(id=asset_id, company=employee.company)
        except Asset.DoesNotExist:
            return Response({'error': 'Asset not found'}, status=status.HTTP_404_NOT_FOUND)

        # ── GPS distance check ──────────────────────────────────────────
        current_zone = asset.current_working_zone
        if current_zone is None:
            return Response({'error': 'This cuba has no current zone assigned'}, status=status.HTTP_400_BAD_REQUEST)

        zone_lat, zone_lon = get_zone_coordinates(current_zone)
        if zone_lat is None:
            return Response({'error': 'Zone has no GPS coordinates configured'}, status=status.HTTP_400_BAD_REQUEST)

        distance = haversine_distance(float(latitude), float(longitude), zone_lat, zone_lon)
        print(f"Asset: {asset.name}")
        print(f"Zone: {current_zone.name} — lat={zone_lat}, lon={zone_lon}")
        print(f"Worker: lat={latitude}, lon={longitude}")
        print(f"Distance: {distance:.1f}m")

        if distance > 50:
            return Response(
                {'error': f'Debes estar a menos de 50m de la cuba. Ahora estás a {int(distance)}m.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        # ───────────────────────────────────────────────────────────────

        transaction = AssetTransaction.objects.create(
            asset=asset,
            company=employee.company,
            from_zone=current_zone,
            to_zone=None,
            transaction_type=TransactionTypeChoices.DELIVERY,
            date=timezone.now(),
            notes=f'Delivered by {employee.full_name}. GPS: {latitude}, {longitude}. Distance: {int(distance)}m',
            firma=firma,
        )

        serializer = AssetTransactionSerializer(transaction)
        return Response(serializer.data, status=status.HTTP_201_CREATED)