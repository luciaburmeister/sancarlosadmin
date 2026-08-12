from django.conf import settings
from django.http import Http404
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from assets.forms import AssetMapTableFilterForm
from assets.models import AssetMap
from assets.services.asset_map import AssetMapService


class AssetMapDetailView(UnfoldModelAdminViewMixin, TemplateView):
    template_name = "admin/assets/assetmap/detail.html"
    permission_required = ("assets.view_assetmap",)
    title = _("Asset Map")

    def dispatch(self, request, *args, **kwargs):
        try:
            self.asset_map = AssetMap.objects.get(pk=kwargs["object_id"])
        except AssetMap.DoesNotExist:
            raise Http404(_("Asset map not found"))
        self.title = self.asset_map.name
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["asset_map"] = self.asset_map
        ctx["map_payload"] = AssetMapService.build_map_payload(self.asset_map)
        ctx["zone_type_legend"] = AssetMapService.zone_type_legend()
        ctx["tile_url"] = settings.LOCATION_FIELD["provider.openstreetmap.tiles"]
        return ctx


class AssetMapTableView(UnfoldModelAdminViewMixin, TemplateView):
    template_name = "admin/assets/assetmap/table.html"
    permission_required = ("assets.view_assetmap",)
    title = _("Asset Map")

    def dispatch(self, request, *args, **kwargs):
        try:
            self.asset_map = AssetMap.objects.get(pk=kwargs["object_id"])
        except AssetMap.DoesNotExist:
            raise Http404(_("Asset map not found"))
        self.title = self.asset_map.name
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        options = AssetMapService.filter_options(self.asset_map)

        filter_form = AssetMapTableFilterForm(
            self.request.GET or None,
            zone_choices=[(z["id"], z["name"]) for z in options["zone_options"]],
            project_file_choices=[(pf["id"], pf["label"]) for pf in options["project_file_options"]],
            dynamic_field_specs=options["dynamic_filters"],
        )

        zone_ids, project_file_ids, search, dynamic_filters = [], [], None, {}
        if filter_form.is_valid():
            zone_ids = filter_form.cleaned_data.get("zone") or []
            project_file_ids = filter_form.cleaned_data.get("expediente") or []
            search = (filter_form.cleaned_data.get("q") or "").strip() or None
            dynamic_filters = {
                spec["key"]: filter_form.cleaned_data.get(f"df_{spec['key']}") or []
                for spec in options["dynamic_filters"]
            }

        rows = AssetMapService.build_table_rows(
            self.asset_map,
            zone_ids=zone_ids,
            project_file_ids=project_file_ids,
            search=search,
            dynamic_filters=dynamic_filters,
        )

        ctx.update({
            "asset_map": self.asset_map,
            "table_data": self._table_data(rows),
            "filter_form": filter_form,
        })
        return ctx

    @staticmethod
    def _table_data(rows):
        """Shapes rows into the {headers, rows} contract unfold/components/table.html expects."""
        table_rows = []
        for row in rows:
            name_cell = format_html('<a href="{}">{}</a>', row["admin_url"], row["name"])

            zone_cell = "—"
            if row["zone_url"]:
                zone_cell = format_html('<a href="{}">{}</a>', row["zone_url"], row["zone_name"])

            project_file_cell = "—"
            if row["project_file_url"]:
                project_file_cell = format_html(
                    '<a href="{}">{}</a>', row["project_file_url"], row["project_file_label"]
                )

            data_cell = "—"
            if row["schema_fields"]:
                data_cell = format_html_join(
                    "",
                    '<div><span class="text-gray-500 dark:text-gray-400">{}:</span> {}</div>',
                    ((f["label"], f["value"]) for f in row["schema_fields"]),
                )

            table_rows.append([
                name_cell,
                zone_cell,
                project_file_cell,
                data_cell,
                row["zone_changed_at"] or "—",
            ])

        return {
            "headers": [
                _("Nombre"), _("Zona"), _("Expediente"), _("Capacidad / Datos"),
                _("Última fecha de colocación"),
            ],
            "rows": table_rows,
        }
