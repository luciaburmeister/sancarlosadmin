from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from unfold.contrib.filters.admin import AutocompleteSelectFilter
from core.admin.company_admin import CompanyRestrictedAdmin
from assets.models import Asset, AssetFault, AssetFaultStatus


@admin.register(AssetFault)
class AssetFaultAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("asset", "fault_type", "severity", "status_badge", "created_at")
    list_filter = ("severity", "status", ("fault_type", AutocompleteSelectFilter))
    autocomplete_fields = ["asset", "fault_type", "company"]
    search_fields = ("asset__name", "fault_type__name")
    readonly_fields = ("created_at", "updated_at")
    actions = ["mark_as_fixed"]

    @display(
        description=_("Status"),
        ordering="status",
        label={
            AssetFaultStatus.PENDING: "warning",
            AssetFaultStatus.FIXED: "success",
        },
    )
    def status_badge(self, obj):
        return obj.status, obj.get_status_display()

    @action(description=_("Mark selected faults as Fixed"))
    def mark_as_fixed(self, request, queryset):
        pending = queryset.filter(status=AssetFaultStatus.PENDING)
        asset_ids = set(pending.values_list("asset_id", flat=True))
        pending.update(status=AssetFaultStatus.FIXED)
        for asset in Asset.objects.filter(pk__in=asset_ids):
            asset.recalculate_operational_status()
