"""
Route optimization for AssetPlanning.

Class-based so the backend (Google, OSRM, straight-line, …) can be swapped.
Usage:
    optimizer = GoogleRouteOptimizer()
    optimizer.optimize_daily_route(employee_id, date)
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date
from typing import Sequence

import requests
from django.conf import settings
from django.contrib.gis.geos import Point
from django.utils import timezone

from assets.models import AssetPlanning, ZoneDistanceCache
from company.models import WorkingZone

logger = logging.getLogger(__name__)


# ── Abstract base ─────────────────────────────────────────────────────────

class BaseRouteOptimizer(ABC):
    """Swap this class to change the distance provider."""

    def optimize_daily_route(self, employee_id: int, day: date) -> None:
        """Reorder all plannings for *employee* on *day* to minimize driving."""
        plannings = list(
            AssetPlanning.objects.filter(
                assigned_to_id=employee_id,
                scheduled_date=day,
                status__in=["pending", "in_transit"],
            )
            .select_related("origin", "destination")
            .order_by("route_order", "pk")
        )
        if len(plannings) <= 1:
            return  # nothing to optimize

        # Collect all zones we need distances between.
        # We chain: planning[0].origin → planning[0].destination,
        # then planning[0].destination → planning[1].origin, etc.
        zones = self._collect_zones(plannings)
        if not zones:
            return

        matrix = self._get_distance_matrix(zones)
        ordered = self._nearest_neighbor(plannings, matrix)

        # Persist new order
        for i, planning in enumerate(ordered):
            planning.route_order = i
        AssetPlanning.objects.bulk_update(ordered, ["route_order"])

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _collect_zones(plannings: list[AssetPlanning]) -> list[WorkingZone]:
        """Deduplicated list of zones involved in plannings."""
        seen = {}
        for p in plannings:
            for z in (p.origin, p.destination):
                if z and z.pk not in seen:
                    seen[z.pk] = z
            for stop in p.stops.select_related("zone").all():
                if stop.zone_id not in seen:
                    seen[stop.zone_id] = stop.zone
        return list(seen.values())

    @abstractmethod
    def _get_distance_matrix(
        self, zones: list[WorkingZone]
    ) -> dict[tuple[int, int], int]:
        """Return {(from_zone_id, to_zone_id): distance_meters}."""

    @staticmethod
    def _nearest_neighbor(
        plannings: list[AssetPlanning],
        matrix: dict[tuple[int, int], int],
    ) -> list[AssetPlanning]:
        """Order plannings so that travel between consecutive ones is minimized.

        Minimizes: planning[i].last_zone → planning[i+1].first_zone
        """
        def _first_zone(p: AssetPlanning) -> int | None:
            return p.origin_id

        def _last_zone(p: AssetPlanning) -> int | None:
            return p.destination_id or p.origin_id

        remaining = list(plannings)
        ordered = [remaining.pop(0)]  # start with the first one

        while remaining:
            last = _last_zone(ordered[-1])
            best_idx, best_dist = 0, float("inf")
            for i, candidate in enumerate(remaining):
                first = _first_zone(candidate)
                if last and first:
                    dist = matrix.get((last, first), float("inf"))
                else:
                    dist = 0  # no zone info → don't penalize
                if dist < best_dist:
                    best_dist = dist
                    best_idx = i
            ordered.append(remaining.pop(best_idx))

        return ordered


# ── Google Routes API implementation ──────────────────────────────────────

def _extract_latlng(zone: WorkingZone) -> tuple[float, float] | None:
    """Extract (lat, lng) from a WorkingZone's location field."""
    loc = zone.location
    if loc is None:
        return None
    # location_field stores as a Point(lng, lat) or "lat,lng" string
    if isinstance(loc, Point):
        return (loc.y, loc.x)
    if isinstance(loc, str) and "," in loc:
        parts = loc.split(",")
        try:
            return (float(parts[0].strip()), float(parts[1].strip()))
        except (ValueError, IndexError):
            return None
    return None


class GoogleRouteOptimizer(BaseRouteOptimizer):
    """Uses Google Routes API computeRouteMatrix for real driving distances."""

    API_URL = "https://routes.googleapis.com/routes/v2:computeRouteMatrix"

    def _get_distance_matrix(
        self, zones: list[WorkingZone]
    ) -> dict[tuple[int, int], int]:
        matrix = {}
        zone_pairs_needed = []

        # Check cache first
        for a in zones:
            for b in zones:
                if a.pk == b.pk:
                    matrix[(a.pk, b.pk)] = 0
                    continue
                cached = ZoneDistanceCache.objects.filter(
                    from_zone=a, to_zone=b
                ).first()
                if cached:
                    matrix[(a.pk, b.pk)] = cached.distance_meters
                else:
                    zone_pairs_needed.append((a, b))

        if not zone_pairs_needed:
            return matrix

        # Fetch from Google Routes API
        api_key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            logger.warning("GOOGLE_MAPS_API_KEY not set — falling back to zero distances")
            for a, b in zone_pairs_needed:
                matrix[(a.pk, b.pk)] = 0
            return matrix

        # Build unique origin/destination lists for the API call
        zone_ids = list({z.pk for z in zones})
        zone_map = {z.pk: z for z in zones}
        latlng_map = {}
        for zid in zone_ids:
            ll = _extract_latlng(zone_map[zid])
            if ll:
                latlng_map[zid] = ll

        # Only include zones with coordinates
        valid_ids = [zid for zid in zone_ids if zid in latlng_map]
        if len(valid_ids) < 2:
            return matrix

        origins = [
            {"waypoint": {"location": {"latLng": {
                "latitude": latlng_map[zid][0],
                "longitude": latlng_map[zid][1],
            }}}}
            for zid in valid_ids
        ]
        # Same list for destinations (full matrix)
        destinations = origins.copy()

        try:
            resp = requests.post(
                self.API_URL,
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": "originIndex,destinationIndex,distanceMeters,duration",
                    "Content-Type": "application/json",
                },
                json={
                    "origins": origins,
                    "destinations": destinations,
                    "travelMode": "DRIVE",
                },
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json()
        except Exception as e:
            logger.error("Google Routes API error: %s", e)
            for a, b in zone_pairs_needed:
                matrix.setdefault((a.pk, b.pk), 0)
            return matrix

        # Parse response — it's a flat list of elements
        for element in results if isinstance(results, list) else []:
            oi = element.get("originIndex", 0)
            di = element.get("destinationIndex", 0)
            dist = element.get("distanceMeters", 0)
            dur_str = element.get("duration", "0s")
            # duration comes as "123s"
            dur = int(dur_str.rstrip("s")) if isinstance(dur_str, str) else 0

            from_zid = valid_ids[oi]
            to_zid = valid_ids[di]
            matrix[(from_zid, to_zid)] = dist

            # Cache it
            ZoneDistanceCache.objects.update_or_create(
                from_zone_id=from_zid,
                to_zone_id=to_zid,
                defaults={
                    "distance_meters": dist,
                    "duration_seconds": dur,
                },
            )

        # Fill any remaining gaps with zero
        for a, b in zone_pairs_needed:
            matrix.setdefault((a.pk, b.pk), 0)

        return matrix
