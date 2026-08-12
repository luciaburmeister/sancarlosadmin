from django.contrib import admin
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from assets.models import AssetMap
from assets.views import AssetMapDetailView, AssetMapTableView


@admin.register(AssetMap)
class AssetMapAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "asset_count", "company", "is_active", "view_map_link", "view_table_link")
    list_filter = ("is_active", "company")
    search_fields = ("name", "description")
    autocomplete_fields = ["assets", "company"]
    fieldsets = (
        (None, {
            "fields": ("name", "description", "company", "is_active", "assets"),
        }),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:object_id>/view/",
                self.admin_site.admin_view(
                    AssetMapDetailView.as_view(model_admin=self)
                ),
                name="assets_assetmap_view",
            ),
            path(
                "<int:object_id>/table/",
                self.admin_site.admin_view(
                    AssetMapTableView.as_view(model_admin=self)
                ),
                name="assets_assetmap_table",
            ),
        ]
        return custom_urls + urls

    @admin.display(description=_("Assets"))
    def asset_count(self, obj):
        return obj.assets.count()

    @admin.display(description=_("Map"))
    def view_map_link(self, obj):
        if obj.pk:
            url = reverse("admin:assets_assetmap_view", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, _("View Map"))
        return "-"

    @admin.display(description=_("Table"))
    def view_table_link(self, obj):
        if obj.pk:
            url = reverse("admin:assets_assetmap_table", args=[obj.pk])
            return format_html('<a href="{}">{}</a>', url, _("View Table"))
        return "-"
