from django.db.models import Q
from django.urls import reverse
from django.utils.formats import date_format

from company.models import WorkingZone
from assets.models import AssetTypeField


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
    def _table_row(cls, asset):
        zone = asset.current_working_zone
        project_file = zone.project_file if zone and zone.project_file_id else None
        return {
            "id": asset.id,
            "name": asset.name,
            "admin_url": reverse("admin:assets_asset_change", args=[asset.id]),
            "zone_name": zone.name if zone else None,
            "zone_url": (
                reverse("admin:company_workingzone_change", args=[zone.id]) if zone else None
            ),
            "project_file_label": str(project_file) if project_file else None,
            "project_file_url": (
                reverse("admin:management_projectfile_change", args=[project_file.id])
                if project_file else None
            ),
            "zone_changed_at": (
                date_format(asset.zone_changed_at, "SHORT_DATETIME_FORMAT")
                if asset.zone_changed_at
                else None
            ),
            "schema_fields": cls._serialize_asset(asset)["schema_fields"],
        }

    @staticmethod
    def _dynamic_field_specs(asset_map):
        """
        AssetTypeField rows for every asset type actually present in this
        map, deduped by key so the same field name shared across asset
        types only produces one filter.
        """
        asset_type_ids = asset_map.assets.values_list("asset_type_id", flat=True).distinct()
        specs = {}
        for spec in AssetTypeField.objects.filter(asset_type_id__in=asset_type_ids).order_by("order", "id"):
            specs.setdefault(spec.name, spec)
        return list(specs.values())

    @classmethod
    def dynamic_filter_options(cls, asset_map):
        """
        One filter descriptor per distinct dynamic field key found on this
        map's assets — works for any asset type's fields, not just a
        hardcoded "capacidad", so it stays correct as new asset types /
        fields get added. Options list only values actually in use.
        """
        specs = cls._dynamic_field_specs(asset_map)
        if not specs:
            return []
        all_data = list(asset_map.assets.values_list("data", flat=True))
        filters = []
        for spec in specs:
            values = sorted({
                str(d.get(spec.name)) for d in all_data if d.get(spec.name) not in (None, "")
            })
            if values:
                filters.append({"key": spec.name, "label": spec.label, "options": values})
        return filters

    @classmethod
    def filter_options(cls, asset_map):
        """
        Choices for every table filter (zona, expediente, one per dynamic
        AssetTypeField key), always computed from the map's full asset set —
        independent of any currently-applied filter — so picking one filter
        never removes options from another, and so the filter form can be
        built/validated before rows are queried.
        """
        zones, project_files = {}, {}
        for asset in asset_map.assets.select_related(
            "current_working_zone", "current_working_zone__project_file"
        ):
            zone = asset.current_working_zone
            if zone:
                zones[zone.id] = zone.name
                if zone.project_file_id:
                    project_files[zone.project_file_id] = str(zone.project_file)

        return {
            "zone_options": [
                {"id": k, "name": v} for k, v in sorted(zones.items(), key=lambda kv: kv[1])
            ],
            "project_file_options": [
                {"id": k, "label": v} for k, v in sorted(project_files.items(), key=lambda kv: kv[1])
            ],
            "dynamic_filters": cls.dynamic_filter_options(asset_map),
        }

    @classmethod
    def build_table_rows(cls, asset_map, *, zone_ids=None, project_file_ids=None, search=None, dynamic_filters=None):
        """
        Tabular, filterable view of the same assets/fields the map shows.

        `zone_ids`/`project_file_ids` are lists (multi-select). `dynamic_filters`
        is {field_key: [values]}, limited by the caller to keys returned from
        `dynamic_filter_options` for this same map.
        """
        assets = (
            asset_map.assets
            .select_related("asset_type", "current_working_zone", "current_working_zone__project_file")
            .prefetch_related("asset_type__fields")
            .order_by("current_working_zone__name", "name")
        )
        if zone_ids:
            assets = assets.filter(current_working_zone_id__in=zone_ids)
        if project_file_ids:
            assets = assets.filter(current_working_zone__project_file_id__in=project_file_ids)
        if search:
            assets = assets.filter(name__icontains=search)
        for key, values in (dynamic_filters or {}).items():
            if values:
                assets = assets.filter(Q(**{f"data__{key}__in": values}))

        return [cls._table_row(asset) for asset in assets]

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
            serialized = cls._serialize_asset(asset)
            if zone is None or lat is None:
                unlocated.append(serialized)
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
            bucket["assets"].append(serialized)

        return {"markers": list(zones_by_id.values()), "unlocated": unlocated}
