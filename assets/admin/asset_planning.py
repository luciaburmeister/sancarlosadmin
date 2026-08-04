from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.db.models import Q
from unfold.admin import ModelAdmin, TabularInline
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.utils.translation import gettext_lazy as _
from unfold.decorators import action, display
from django.contrib import messages
from company.models import Employee
from assets.constants import SERVICE_TYPE_MAQUINA_PROPIA
from assets.forms import StaticSelect2MultipleWidget
from assets.models import Asset, AssetPlanning, AssetPlanningStop, AssetTypeCategory, AssignmentOrigin

CONDITIONAL_FIELDS = {
    "machinist_moved": f"service_type == '{SERVICE_TYPE_MAQUINA_PROPIA}'",
    "moved_employees": f"service_type == '{SERVICE_TYPE_MAQUINA_PROPIA}' && machinist_moved",
}


class AssetPlanningStopInline(TabularInline):

    model = AssetPlanningStop
    extra = 1
    fields = ("sequence", "zone", "notes")
    autocomplete_fields = ["zone"]
    ordering = ["sequence"]


class TraceabilityFilter(admin.SimpleListFilter):
    """Filters on the derived Pending/Conflict/Auto-assigned/Manual status (see AssetPlanning.traceability_label)."""
    title = _("Assignment Status")
    parameter_name = "traceability"

    def lookups(self, request, model_admin):
        return [
            ("pending", _("Pending")),
            ("conflict", _("Conflict")),
            ("auto", _("Auto-assigned")),
            ("manual", _("Manual")),
        ]

    def queryset(self, request, queryset):
        value = self.value()
        if value is None:
            return queryset
        if value == "pending":
            return queryset.filter(
                Q(assigned_to__isnull=True) | Q(transport_asset__isnull=True) | Q(scheduled_time__isnull=True)
            )

        fully_assigned = queryset.filter(
            assigned_to__isnull=False, transport_asset__isnull=False, scheduled_time__isnull=False,
        )
        if value == "conflict":
            ids = [p.pk for p in fully_assigned if p.has_conflict]
        elif value == "auto":
            ids = [
                p.pk for p in fully_assigned.filter(assignment_origin=AssignmentOrigin.AUTO)
                if not p.has_conflict
            ]
        elif value == "manual":
            ids = [
                p.pk for p in fully_assigned.exclude(assignment_origin=AssignmentOrigin.AUTO)
                if not p.has_conflict
            ]
        else:
            return queryset
        return queryset.filter(pk__in=ids)


@admin.register(AssetPlanning)
class AssetPlanningAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = (
        "asset", "origin", "destination",
        "assigned_to", "transport_asset",
        "scheduled_date", "scheduled_time", "urgency", "show_traceability",
        "route_order", "status", "service_type", "company",
    )
    list_filter = (TraceabilityFilter, "status", "urgency", "company", "origin", "destination", "service_type")
    search_fields = (
        "asset__name",
        "assigned_to__first_name",
        "assigned_to__last_name",
        "origin__name",
        "destination__name",
        "transport_asset__name",
    )
    autocomplete_fields = ["asset", "origin", "destination", "assigned_to", "transport_asset", "suggested_trucks", "project_file"]
    readonly_fields = ["assignment_origin"]
    inlines = [AssetPlanningStopInline]
    actions_list = ["auto_assign_action", "optimize_daily_routes_action"]
    conditional_fields = CONDITIONAL_FIELDS

    class Media(StaticSelect2MultipleWidget.Media):
        pass
    fieldsets = (
        (_("Company"), {
            "fields": (
                "company",
                "project_file",
            )
        }),
        (_("Asset"), {
            "fields": (
                "asset",
            )
        }),
        (_("Route"), {
            "fields": (
                ("origin", "destination"),
                ("scheduled_date", "scheduled_time", "scheduled_end_time"),
            )
        }),
        (_("Assignment"), {
            "fields": (
                ("assigned_to", "transport_asset"),
                "suggested_trucks",
                ("status", "route_order", "urgency"),
                "assignment_origin",
                ("planning_type", "service_type"),
                ("cuba_operation_type", "cuba_size"),
                ("machinist_moved", "moved_employees"),
                "load_description",
                "notes",
            )
        }),
    )

    def save_model(self, request, obj, form, change):
        if not obj.company_id and obj.asset and obj.asset.company_id:
            obj.company = obj.asset.company
        super().save_model(request, obj, form, change)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        obj = getattr(request, "_obj_", None)
        if db_field.name == "suggested_trucks":
            qs = Asset.objects.filter(asset_type__category=AssetTypeCategory.TRUCK, is_active=True)
            if obj is not None and obj.company_id:
                qs = qs.filter(company=obj.company_id)
            kwargs["queryset"] = qs.order_by("name")
        elif db_field.name == "moved_employees":
            qs = Employee.objects.filter(is_active=True)
            if obj is not None and obj.company_id:
                qs = qs.filter(company=obj.company_id)
            kwargs["queryset"] = qs.order_by("first_name", "last_name")
            kwargs["widget"] = StaticSelect2MultipleWidget()
        form_field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "moved_employees" and form_field is not None:
            widget = StaticSelect2MultipleWidget()
            if isinstance(form_field.widget, RelatedFieldWidgetWrapper):
                form_field.widget.widget = widget
            else:
                form_field.widget = widget
            form_field.help_text = ""
        return form_field

    def get_form(self, request, obj=None, **kwargs):
        # Stashed for formfield_for_manytomany above.
        request._obj_ = obj
        return super().get_form(request, obj, **kwargs)

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

    @action(
        description=_("Auto-assign Driver/Truck"),
        url_path="auto-assign",
        icon="auto_awesome",
    )
    def auto_assign_action(self, request):
        """Run the v1 greedy auto-assigner over every date currently visible in the changelist."""
        from assets.services.auto_assigner import SimpleAssigner

        qs = self.get_queryset(request).filter(status="pending").filter(
            Q(assigned_to__isnull=True) | Q(transport_asset__isnull=True)
        )
        pairs = qs.values_list("company_id", "scheduled_date").distinct()

        assigner = SimpleAssigner()
        total_assigned, total_skipped = 0, 0
        for company_id, day in pairs:
            result = assigner.assign_for_date(day, company_id)
            total_assigned += len(result.assigned)
            total_skipped += len(result.skipped)

        if total_assigned:
            messages.success(request, _("Auto-assigned %(count)d planning(s).") % {"count": total_assigned})
        if total_skipped:
            messages.warning(
                request,
                _("%(count)d planning(s) could not be auto-assigned — no driver/truck available without conflicts.")
                % {"count": total_skipped},
            )
        if not total_assigned and not total_skipped:
            messages.info(request, _("Nothing to auto-assign — every visible planning already has a driver and truck."))

        from django.shortcuts import redirect
        return redirect(request.META.get("HTTP_REFERER", ".."))

    def has_auto_assign_action_permission(self, request):
        return request.user.has_perm("assets.change_assetplanning")

    @display(
        description=_("Assignment"),
        label={
            _("Pending"): "warning",
            _("Conflict"): "danger",
            _("Auto-assigned"): "info",
            _("Manual"): "success",
        },
    )
    def show_traceability(self, obj):
        return obj.traceability_label