from django.urls import reverse
from django.utils.formats import date_format

from company.models import WorkingZone


class AssetMapService:
    """Builds the marker payload used to render an AssetMap on a Leaflet map."""

    ZONE_TYPE_COLORS = {
        WorkingZone.ZoneType.OBRA: "#f59e0b",
        WorkingZone.ZoneType.VERTEDERO: "#ef4444",
        WorkingZone.ZoneType.ALMACEN: "#6366f1",
    }
    DEFAULT_ZONE_COLOR = "#6b7280"

    @classmethod
    def zone_color(cls, zone_type):
        return cls.ZONE_TYPE_COLORS.get(zone_type, cls.DEFAULT_ZONE_COLOR)

    @classmethod
    def zone_type_legend(cls):
        """List of {value, label, color} for every zone type, for a map legend."""
        return [
            {"value": value, "label": str(label), "color": cls.zone_color(value)}
            for value, label in WorkingZone.ZoneType.choices
        ]

    @staticmethod
    def _serialize_asset(asset):
        identifiers = [f"{i.label}: {i.value}" for i in asset.identifiers.all()]
        schema_fields = []
        for spec in asset.asset_type.fields.all():
            value = asset.data.get(spec.name)
            if value not in (None, ""):
                schema_fields.append({"label": spec.label, "value": value})
        return {
            "id": asset.id,
            "name": asset.name,
            "asset_type": asset.asset_type.name,
            "admin_url": reverse("admin:assets_asset_change", args=[asset.id]),
            "identifiers": identifiers,
            "schema_fields": schema_fields,
            "zone_changed_at": (
                date_format(asset.zone_changed_at, "SHORT_DATETIME_FORMAT")
                if asset.zone_changed_at
                else None
            ),
        }

    @classmethod
    def build_map_payload(cls, asset_map):
        """
        Resolves each asset's marker position from its current_working_zone and
        groups assets that share a zone into a single marker, since position is
        always derived (never stored per-map) and several assets commonly sit at
        the same zone.

        Returns {"markers": [...], "unlocated": [...]}: `markers` has one entry
        per zone with resolvable coordinates ({zone_id, zone_name, zone_type,
        zone_type_label, color, lat, lng, assets}); `unlocated` lists assets
        with no zone, or a zone with no resolvable location.
        """
        assets = (
            asset_map.assets
            .select_related("asset_type", "current_working_zone")
            .prefetch_related("identifiers", "asset_type__fields")
            .order_by("name")
        )

        zones_by_id = {}
        unlocated = []
        for asset in assets:
            zone = asset.current_working_zone
            lat = lng = None
            if zone is not None:
                lat, lng = zone.get_coordinates()
            if zone is None or lat is None:
                unlocated.append(cls._serialize_asset(asset))
                continue
            bucket = zones_by_id.setdefault(
                zone.id,
                {
                    "zone_id": zone.id,
                    "zone_name": zone.name,
                    "zone_type": zone.zone_type,
                    "zone_type_label": zone.get_zone_type_display(),
                    "color": cls.zone_color(zone.zone_type),
                    "lat": lat,
                    "lng": lng,
                    "assets": [],
                },
            )
            bucket["assets"].append(cls._serialize_asset(asset))

        return {"markers": list(zones_by_id.values()), "unlocated": unlocated}
