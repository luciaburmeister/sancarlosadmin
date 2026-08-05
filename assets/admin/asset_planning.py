from datetime import timedelta

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.urls import reverse
from unfold.admin import ModelAdmin, TabularInline
from unfold.forms import BaseDialogForm
from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.utils.translation import gettext_lazy as _
from unfold.decorators import action, display
from django.contrib import messages
from company.models import Employee
from assets.constants import SERVICE_TYPE_MAQUINA_PROPIA
from assets.forms import EstimatedDurationChoiceField, TimeChoiceField
from assets.models import Asset, AssetPlanning, AssetPlanningStop, AssetTypeCategory, AssignmentOrigin


class AutoAssignBulkForm(BaseDialogForm):
    form_after_template = "assets/auto_assign_chips.html"

    start_date = forms.DateField(label=_("From"), widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(label=_("To"), widget=forms.DateInput(attrs={"type": "date"}))
    reassign_all = forms.BooleanField(
        required=False, initial=False, label=_("Reassign all plans"),
        help_text=_(
            "Also re-run plannings that already have a truck/driver (keeps the "
            "existing date and time, only redoes the truck/driver). Leave "
            "unchecked to only fill in plannings still missing a truck, "
            "driver, or time."
        ),
    )

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            raise ValidationError({"end_date": _("End date must be on or after the start date.")})
        return cleaned

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
    list_filter = (TraceabilityFilter, "status", "urgency", "service_type", "company", "origin", "destination")
    search_fields = (
        "asset__name",
        "assigned_to__first_name",
        "assigned_to__last_name",
        "origin__name",
        "destination__name",
        "transport_asset__name",
    )
    autocomplete_fields = [
        "company",
        "project_file",
        "asset",
        "origin",
        "destination",
        "assigned_to",
        "transport_asset",
        "suggested_trucks",
        "moved_employees",
    ]
    readonly_fields = ["assignment_origin", "computed_end_time_display"]
    inlines = [AssetPlanningStopInline]
    actions_list = ["auto_assign_action", "optimize_daily_routes_action"]
    actions_row = ["auto_assign_row_action"]
    conditional_fields = CONDITIONAL_FIELDS
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
                ("scheduled_date", "scheduled_time"),
                ("estimated_duration", "computed_end_time_display"),
            )
        }),
        (_("Assignment"), {
            "fields": (
                ("assigned_to", "transport_asset"),
                "suggested_trucks",
                ("status", "route_order", "urgency"),
                "assignment_origin",
                ("skip_auto_assignment", "time_locked"),
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

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "estimated_duration":
            return EstimatedDurationChoiceField(
                label=db_field.verbose_name,
                help_text=db_field.help_text,
            )
        if db_field.name == "scheduled_time":
            return TimeChoiceField(
                label=db_field.verbose_name,
                help_text=db_field.help_text,
            )
        return super().formfield_for_dbfield(db_field, request, **kwargs)

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
        return super().formfield_for_manytomany(db_field, request, **kwargs)

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
        dialog={
            "title": _("Auto-assign plannings"),
            "description": _(
                "Looks for a compatible truck, its driver, and a free time for "
                "pending plannings in the selected date range. This can't be "
                "undone automatically."
            ),
            "form_class": AutoAssignBulkForm,
            "form_submit_text": _("Auto-assign"),
        },
    )
    def auto_assign_action(self, request, form):
        """Run TransportAssigner over every date in the chosen range, for every company the user can see."""
        from assets.services.transport_assigner import TransportAssigner

        start_date = form.cleaned_data["start_date"]
        end_date = form.cleaned_data["end_date"]
        reassign_all = form.cleaned_data["reassign_all"]
        dates = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

        assigner = TransportAssigner()
        total_assigned, total_skipped = 0, 0
        for company_id in get_user_companies(request.user).values_list("id", flat=True):
            result = assigner.assign_bulk(dates, company_id, reassign_all=reassign_all)
            total_assigned += len(result.assigned)
            total_skipped += len(result.skipped)

        if total_assigned:
            messages.success(request, _("Auto-assigned %(count)d planning(s).") % {"count": total_assigned})
        if total_skipped:
            messages.warning(
                request,
                _("%(count)d planning(s) could not be auto-assigned — no compatible truck/driver/time available.")
                % {"count": total_skipped},
            )
        if not total_assigned and not total_skipped:
            messages.info(request, _("Nothing to auto-assign in that date range."))

        # Submitted via htmx from inside the dialog modal — a real redirect
        # gets auto-followed before htmx's JS ever sees it, so HX-Redirect
        # must be set on a plain 200 response instead.
        response = HttpResponse(status=200)
        response["HX-Redirect"] = request.META.get("HTTP_REFERER", reverse("admin:assets_assetplanning_changelist"))
        return response

    def has_auto_assign_action_permission(self, request):
        return request.user.has_perm("assets.change_assetplanning")

    @action(
        description=_("Auto-assign"),
        url_path="auto-assign-one",
        icon="auto_awesome",
        dialog={
            "title": _("Auto-assign this planning?"),
            "description": _(
                "Looks for a compatible truck, its driver, and a free time for "
                "this planning automatically, respecting every other planning "
                "as-is."
            ),
            "form_submit_text": _("Auto-assign"),
        },
    )
    def auto_assign_row_action(self, request, _form, object_id):
        from assets.services.transport_assigner import TransportAssigner

        planning = self.get_object(request, object_id)
        if planning is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)

        if TransportAssigner().assign_one(planning):
            messages.success(request, _("Planning auto-assigned."))
        else:
            messages.warning(
                request, _("Could not auto-assign — no compatible truck/driver/time available."),
            )

        response = HttpResponse(status=200)
        response["HX-Redirect"] = request.META.get(
            "HTTP_REFERER", reverse("admin:assets_assetplanning_change", args=[object_id])
        )
        return response

    def has_auto_assign_row_action_permission(self, request):
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

    def computed_end_time_display(self, obj):
        return obj.scheduled_end_time or "—"
    computed_end_time_display.short_description = _("Computed End Time")