from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.utils.translation import gettext_lazy as _
from unfold.decorators import action
from django.contrib import messages
from assets.models import AssetPlanning, AssetPlanningStop


class AssetPlanningStopInline(TabularInline):
    
    model = AssetPlanningStop
    extra = 1
    fields = ("sequence", "zone", "notes")
    autocomplete_fields = ["zone"]
    ordering = ["sequence"]


@admin.register(AssetPlanning)
class AssetPlanningAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = (
        "asset", "origin", "destination",
        "assigned_to", "transport_asset",
        "scheduled_date", "route_order", "status", "planning_type", "company",
    )
    list_filter = ("status", "company", "origin", "destination", "planning_type")
    search_fields = (
        "asset__name",
        "assigned_to__first_name",
        "assigned_to__last_name",
        "origin__name",
        "destination__name",
        "transport_asset__name",
    )
    autocomplete_fields = ["asset", "origin", "destination", "assigned_to", "transport_asset"]
    exclude = ["company"]
    inlines = [AssetPlanningStopInline]
    actions_list = ["optimize_daily_routes_action"]
    fieldsets = (
        (_("Asset"), {
            "fields": (
                "asset",
            )
        }),
        (_("Route"), {
            "fields": (
                ("origin", "destination"),
                "scheduled_date",
            )
        }),
        (_("Assignment"), {
            "fields": (
                ("assigned_to", "transport_asset"),
                ("status", "route_order"),
                ("planning_type",),
                "load_description",
                "notes",
            )
        }),
    )

    def save_model(self, request, obj, form, change):
        if obj.asset and obj.asset.company:
            obj.company = obj.asset.company
        super().save_model(request, obj, form, change)

    @action(
        description=_("Optimize Daily Routes"),
        url_path="optimize-daily-routes",
        icon="route",
    )
    def optimize_daily_routes_action(self, request):
        """Optimize route order for all plannings of each employee on each date."""
        from assets.services.route_optimizer import GoogleRouteOptimizer
        optimizer = GoogleRouteOptimizer()
        qs = self.get_queryset(request).filter(
            status__in=["pending", "in_transit"],
            assigned_to__isnull=False,
        ).values_list("assigned_to", "scheduled_date").distinct()
        optimized = 0
        for employee_id, date in qs:
            try:
                optimizer.optimize_daily_route(employee_id, date)
                optimized += 1
            except Exception as e:
                messages.warning(request, f"Failed to optimize route for employee {employee_id} on {date}: {e}")
        messages.success(request, _("Optimized %(count)d daily route(s).") % {"count": optimized})
        from django.shortcuts import redirect
        return redirect(request.META.get("HTTP_REFERER", ".."))

    def has_optimize_daily_routes_action_permission(self, request):
        return request.user.has_perm("assets.change_assetplanning")