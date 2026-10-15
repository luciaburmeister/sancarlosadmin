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
from datetime import date, timedelta
from typing import Sequence

import requests
from django.conf import settings
from django.contrib.gis.geos import Point
from django.utils import timezone

from assets.models import AssetPlanning, ZoneDistanceCache
from company.models import WorkingZone

logger = logging.getLogger(__name__)

DEFAULT_ESTIMATED_DURATION = timedelta(hours=1)


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

        matrix = self.get_distance_matrix(zones)
        ordered = self._nearest_neighbor(plannings, matrix)

        # Persist new order
        for i, planning in enumerate(ordered):
            planning.route_order = i
        AssetPlanning.objects.bulk_update(ordered, ["route_order"])

    def estimate_duration(
        self,
        from_zone: WorkingZone | None,
        to_zone: WorkingZone | None,
        default: timedelta = DEFAULT_ESTIMATED_DURATION,
    ) -> timedelta:
        """Falls back to `default` when zones are missing or have no route data."""
        if not from_zone or not to_zone:
            return default
        return self.estimate_durations([(from_zone, to_zone)], default=default)[(from_zone.pk, to_zone.pk)]

    def estimate_durations(
        self,
        pairs: Sequence[tuple[WorkingZone | None, WorkingZone | None]],
        default: timedelta = DEFAULT_ESTIMATED_DURATION,
    ) -> dict[tuple[int, int], timedelta]:
        """
        Driving time for every (from, to) pair, keyed by (from_id, to_id).

        One cache query for all pairs and at most one provider round-trip for
        the pairs the cache lacks, so a page listing N plannings costs the
        same as one. Pairs with a missing zone, or still unknown after the
        provider was asked, get `default`.
        """
        wanted: dict[tuple[int, int], tuple[WorkingZone, WorkingZone]] = {}
        for a, b in pairs:
            if a and b:
                wanted[(a.pk, b.pk)] = (a, b)
        if not wanted:
            return {}

        seconds: dict[tuple[int, int], int] = {}
        for key, entry in self._cached_entries(wanted.keys()).items():
            if entry.duration_seconds:
                seconds[key] = entry.duration_seconds
        missing = [wanted[key] for key in wanted if key not in seconds and key[0] != key[1]]
        if missing:
            for key, (_distance, duration) in self._fetch_pairs(missing).items():
                if duration:
                    seconds[key] = duration
        return {
            key: timedelta(seconds=seconds[key]) if key in seconds else default
            for key in wanted
        }

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _cached_entries(keys) -> dict[tuple[int, int], ZoneDistanceCache]:
        """Cache rows for the given (from_id, to_id) keys, in one query."""
        keys = set(keys)
        if not keys:
            return {}
        rows = ZoneDistanceCache.objects.filter(
            from_zone_id__in={k[0] for k in keys}, to_zone_id__in={k[1] for k in keys},
        )
        return {
            (row.from_zone_id, row.to_zone_id): row
            for row in rows
            if (row.from_zone_id, row.to_zone_id) in keys
        }

    def get_distance_matrix(
        self, zones: list[WorkingZone]
    ) -> dict[tuple[int, int], int]:
        """Return {(from_zone_id, to_zone_id): distance_meters} for every pair.

        One cache query for all pairs, then one provider call for the uncached
        ones (chunked to the provider's limit): callers with several zone
        pairs collect them and call this once rather than looping.
        """
        matrix: dict[tuple[int, int], int] = {}
        keys = [(a.pk, b.pk) for a in zones for b in zones if a.pk != b.pk]
        cached = self._cached_entries(keys)
        needed = []
        for a in zones:
            for b in zones:
                if a.pk == b.pk:
                    matrix[(a.pk, b.pk)] = 0
                elif (a.pk, b.pk) in cached:
                    matrix[(a.pk, b.pk)] = cached[(a.pk, b.pk)].distance_meters
                else:
                    needed.append((a, b))
        if needed:
            fetched = self._fetch_pairs(needed)
            for a, b in needed:
                matrix[(a.pk, b.pk)] = fetched.get((a.pk, b.pk), (0, 0))[0]
        return matrix

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
    def _fetch_pairs(
        self, pairs: list[tuple[WorkingZone, WorkingZone]]
    ) -> dict[tuple[int, int], tuple[int, int]]:
        """
        Ask the provider for the pairs the cache lacks and cache the answers.
        Returns {(from_zone_id, to_zone_id): (distance_meters, duration_seconds)};
        pairs the provider could not resolve are simply absent.
        """

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
    # computeRouteMatrix accepts at most 625 elements per request; a square
    # block of this many origins and destinations stays under it.
    MAX_SIDE = 25

    def _fetch_pairs(
        self, pairs: list[tuple[WorkingZone, WorkingZone]]
    ) -> dict[tuple[int, int], tuple[int, int]]:
        api_key = getattr(settings, "GOOGLE_MAPS_API_KEY", "")
        if not api_key:
            logger.warning("GOOGLE_MAPS_API_KEY not set — falling back to zero distances")
            return {}

        origins: dict[int, WorkingZone] = {}
        destinations: dict[int, WorkingZone] = {}
        for a, b in pairs:
            if _extract_latlng(a) and _extract_latlng(b):
                origins.setdefault(a.pk, a)
                destinations.setdefault(b.pk, b)
        if not origins or not destinations:
            return {}

        results: dict[tuple[int, int], tuple[int, int]] = {}
        origin_list, destination_list = list(origins.values()), list(destinations.values())
        for i in range(0, len(origin_list), self.MAX_SIDE):
            for j in range(0, len(destination_list), self.MAX_SIDE):
                results.update(self._fetch_block(
                    api_key,
                    origin_list[i:i + self.MAX_SIDE],
                    destination_list[j:j + self.MAX_SIDE],
                ))
        return results

    def _fetch_block(
        self, api_key: str, origins: list[WorkingZone], destinations: list[WorkingZone]
    ) -> dict[tuple[int, int], tuple[int, int]]:
        def waypoints(zones):
            return [
                {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}
                for lat, lng in (_extract_latlng(z) for z in zones)
            ]

        try:
            resp = requests.post(
                self.API_URL,
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": "originIndex,destinationIndex,distanceMeters,duration",
                    "Content-Type": "application/json",
                },
                json={
                    "origins": waypoints(origins),
                    "destinations": waypoints(destinations),
                    "travelMode": "DRIVE",
                },
                timeout=30,
            )
            resp.raise_for_status()
            elements = resp.json()
        except Exception as e:
            logger.error("Google Routes API error: %s", e)
            return {}

        # The response is a flat list of elements, one per origin/destination pair.
        results: dict[tuple[int, int], tuple[int, int]] = {}
        for element in elements if isinstance(elements, list) else []:
            from_zid = origins[element.get("originIndex", 0)].pk
            to_zid = destinations[element.get("destinationIndex", 0)].pk
            if from_zid == to_zid:
                continue
            dist = element.get("distanceMeters", 0)
            dur_str = element.get("duration", "0s")  # comes as "123s"
            dur = int(dur_str.rstrip("s")) if isinstance(dur_str, str) else 0
            results[(from_zid, to_zid)] = (dist, dur)
            ZoneDistanceCache.objects.update_or_create(
                from_zone_id=from_zid,
                to_zone_id=to_zid,
                defaults={"distance_meters": dist, "duration_seconds": dur},
            )
        return results
