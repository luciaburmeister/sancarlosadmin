"""Suggest and hold an empty warehouse cuba for a plan via reserved_asset.

Does not touch AssetPlanning.asset — that is set only by the QR-scan pickup.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from api.fleet.utils import haversine_distance
from assets.constants import CUBA_CANDIDATE_LIMIT
from assets.models import Asset, AssetDataKey, AssetPlanning
from company.models import WorkingZone


class CubaReservationConflict(Exception):
    def __init__(self, remaining_candidates):
        self.remaining_candidates = remaining_candidates
        super().__init__("This cuba was reserved or picked up by another driver in the meantime.")


@dataclass
class CubaCandidate:
    asset: Asset
    distance_m: float | None


def find_candidates(planning: AssetPlanning, limit: int = CUBA_CANDIDATE_LIMIT) -> list[CubaCandidate]:
    locked_ids = AssetPlanning.locked_cuba_ids(planning.company, exclude_planning_id=planning.pk)
    pool = (
        Asset.objects.select_related("current_working_zone")
        .filter(
            company=planning.company,
            is_active=True,
            current_working_zone__zone_type=WorkingZone.ZoneType.ALMACEN,
            data__has_key=AssetDataKey.CAPACIDAD,
        )
        .exclude(pk__in=locked_ids)
        .prefetch_related("transactions")
    )
    if planning.transport_asset_id:
        pool = pool.exclude(pk__in=planning.transport_asset.excluded_assets.values_list("pk", flat=True))

    dest_lat = dest_lon = None
    if planning.destination_id:
        dest_lat, dest_lon = planning.destination.get_coordinates()

    candidates = []
    for asset in pool:
        if not planning.matches_cuba_size(asset.capacidad):
            continue
        if not asset.is_currently_empty():
            continue
        distance_m = None
        if dest_lat is not None:
            lat, lon = asset.current_working_zone.get_coordinates()
            if lat is not None:
                distance_m = haversine_distance(dest_lat, dest_lon, lat, lon)
        candidates.append(CubaCandidate(asset=asset, distance_m=distance_m))

    candidates.sort(key=lambda c: (c.distance_m is None, c.distance_m))
    return candidates[:limit]


def reserve_cuba(planning: AssetPlanning, asset: Asset) -> AssetPlanning:
    with transaction.atomic():
        locked_asset = Asset.objects.select_for_update(skip_locked=True).filter(pk=asset.pk).first()
        if locked_asset is None:
            raise CubaReservationConflict(find_candidates(planning))

        still_locked_ids = AssetPlanning.locked_cuba_ids(planning.company, exclude_planning_id=planning.pk)
        if locked_asset.pk in still_locked_ids or not locked_asset.is_currently_empty():
            raise CubaReservationConflict(find_candidates(planning))

        planning = AssetPlanning.objects.select_for_update().get(pk=planning.pk)
        planning.reserved_asset = locked_asset
        planning.save(update_fields=["reserved_asset"])
    return planning


def release_stale_reservations() -> list[int]:
    stale = AssetPlanning.objects.filter(
        reserved_asset__isnull=False,
        scheduled_date__lt=timezone.localdate(),
    )
    ids = list(stale.values_list("id", flat=True))
    stale.update(reserved_asset=None)
    return ids
