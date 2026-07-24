from django.conf import settings
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

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
