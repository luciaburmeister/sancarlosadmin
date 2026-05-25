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
    try:
        loc = zone.location
        if hasattr(loc, "x") and hasattr(loc, "y"):
            return loc.y, loc.x
        point_str = str(loc)
        if ";" in point_str:
            point_str = point_str.split(";")[1]
        coords = point_str.strip().replace("POINT (", "").replace("POINT(", "").replace(")", "").split()
        lon = float(coords[0])
        lat = float(coords[1])
        return lat, lon
    except Exception as e:
        print(f"get_zone_coordinates error for zone {zone}: {e}")
        return None, None


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
