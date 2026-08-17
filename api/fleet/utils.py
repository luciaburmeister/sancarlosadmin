import math

from django.utils import timezone

from company.models import WorkingZone

from api.constants import MAX_ZONE_RADIUS_M


def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_zone_coordinates(zone):
    return zone.get_coordinates()


def find_zone_within_radius(latitude, longitude, company, radius_metres=MAX_ZONE_RADIUS_M):
    zones = WorkingZone.objects.filter(company=company, is_active=True)
    for zone in zones:
        zone_lat, zone_lon = get_zone_coordinates(zone)
        if zone_lat is None:
            continue
        distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
        if distance <= radius_metres:
            return zone, distance
    return None, None


def find_zones_within_radius(
    latitude, longitude, company,
    radius_metres=MAX_ZONE_RADIUS_M,
    zone_type=None,
    zone_types=None,
    unconfirmed_only=False,
):
    """
    Returns all zones within radius sorted by distance (closest first).

    zone_type: filter to a single zone type (e.g. WorkingZone.ZoneType.VERTEDERO)
    zone_types: filter to a list of zone types (e.g. [OBRA, ALMACEN])
                preferred over zone_type when both are provided
    unconfirmed_only: restrict to zones whose location has never been
                confirmed by a real transaction (used for the wider
                first-transaction radius fallback)
    """
    zones = WorkingZone.objects.filter(company=company, is_active=True)
    if zone_types is not None:
        zones = zones.filter(zone_type__in=zone_types)
    elif zone_type is not None:
        zones = zones.filter(zone_type=zone_type)
    if unconfirmed_only:
        zones = zones.filter(location_confirmed_at__isnull=True)
    results = []
    for zone in zones:
        zone_lat, zone_lon = get_zone_coordinates(zone)
        if zone_lat is None:
            continue
        distance = haversine_distance(latitude, longitude, zone_lat, zone_lon)
        if distance <= radius_metres:
            results.append((zone, distance))
    results.sort(key=lambda x: x[1])
    return results


def confirm_zone_location(zone, latitude, longitude):
    """
    Locks in a zone's real-world GPS location from its first transaction,
    overwriting the planner's rough initial pin. Only meant to be called
    once per zone (location_confirmed_at is None) — after this, the zone
    goes back to the normal strict geofence radius.
    """
    zone.location = f"POINT({longitude} {latitude})"
    zone.location_confirmed_at = timezone.now()
    zone.save(update_fields=["location", "location_confirmed_at"])