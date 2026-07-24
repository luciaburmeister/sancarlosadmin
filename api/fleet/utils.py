import math

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
    zone_types=None
):
    """
    Returns all zones within radius sorted by distance (closest first).

    zone_type: filter to a single zone type (e.g. WorkingZone.ZoneType.VERTEDERO)
    zone_types: filter to a list of zone types (e.g. [OBRA, ALMACEN])
                preferred over zone_type when both are provided
    """
    zones = WorkingZone.objects.filter(company=company, is_active=True)
    if zone_types is not None:
        zones = zones.filter(zone_type__in=zone_types)
    elif zone_type is not None:
        zones = zones.filter(zone_type=zone_type)
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