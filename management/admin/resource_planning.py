from unfold.contrib.filters.admin import RangeDateFilter
from unfold.contrib.filters.admin import MultipleChoicesDropdownFilter
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from core.admin.company_admin import CompanyRestrictedAdmin
from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter

from management.models import (
    ResourcePlanning,
    ResourcePlanningType,
    ResourcePlanningStatus,
)
from management.views.resource_planning_schedule import ResourcePlanningScheduleView

@admin.register(ResourcePlanningType)
class ResourcePlanningTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "color_swatch", "icon", "is_active", "company"]
    list_filter = ["is_active", ("company", AutocompleteSelectMultipleFilter)]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    list_select_related = ["company"]

    fieldsets = (
        (None, {
            "fields": (
                "company",
                "name",
                ("color", "icon"),
                "is_active",
            )
        }),
    )

    @display(description=_("Color"))
    def color_swatch(self, obj):
        return format_html(
            '<span style="display:inline-block; width:20px; height:20px; '
            'border-radius:4px; background:{};" title="{}"></span>',
            obj.color, obj.color,
        )


# ── Resource Planning ──────────────────────────────────────────────────────

# Whitelist of models that can be planned
PLANNABLE_MODELS = ["employee", "machine", "asset"]


@admin.register(ResourcePlanning)
class ResourcePlanningAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = [
        "type_badge",
        "resource_display",
        "project_file",
        "start",
        "end",
        "show_status",
        "company",
    ]
    list_filter = [
        ("company",AutocompleteSelectMultipleFilter),
        ("status",MultipleChoicesDropdownFilter),
        ("planning_type", AutocompleteSelectMultipleFilter),
        ("start",RangeDateFilter),
    ]
    search_fields = ["project_file__name", "project_file__reference", "notes"]
    autocomplete_fields = ["company", "project_file", "planning_type"]
    list_select_related = ["project_file", "planning_type", "company", "resource_content_type"]
    actions_list = ["schedule_view_action"]

    def get_search_results(self, request, queryset, search_term):
        queryset, may_have_duplicates = super().get_search_results(request, queryset, search_term)
        if search_term:
            from django.db.models import Q
            from company.models import Employee, Machine
            from assets.models import Asset

            employee_ct = ContentType.objects.get_for_model(Employee)
            machine_ct = ContentType.objects.get_for_model(Machine)
            asset_ct = ContentType.objects.get_for_model(Asset)

            emp_ids = Employee.objects.filter(
                Q(first_name__icontains=search_term) | Q(last_name__icontains=search_term)
            ).values_list("pk", flat=True)
            machine_ids = Machine.objects.filter(
                Q(name__icontains=search_term) | Q(code__icontains=search_term)
            ).values_list("pk", flat=True)
            asset_ids = Asset.objects.filter(
                name__icontains=search_term
            ).values_list("pk", flat=True)

            resource_q = (
                Q(resource_content_type=employee_ct, resource_object_id__in=emp_ids)
                | Q(resource_content_type=machine_ct, resource_object_id__in=machine_ids)
                | Q(resource_content_type=asset_ct, resource_object_id__in=asset_ids)
            )
            queryset = queryset | self.model.objects.filter(resource_q)
            may_have_duplicates = True
        return queryset, may_have_duplicates

    fieldsets = (
        (_("Planning"), {
            "fields": (
                "company",
                "project_file",
                "planning_type",
                "status",
            )
        }),
        (_("Resource"), {
            "fields": (
                "resource_content_type",
                "resource_object_id",
            )
        }),
        (_("Schedule"), {
            "fields": (
                ("start", "end"),
            )
        }),
        (_("Notes"), {
            "fields": (
                "notes",
            )
        }),
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "resource_content_type":
            kwargs["queryset"] = ContentType.objects.filter(
                model__in=PLANNABLE_MODELS,
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @display(description=_("Type"))
    def type_badge(self, obj):
        if not obj.planning_type:
            return "—"
        return format_html(
            '<span style="display:inline-flex; align-items:center; gap:4px;'
            ' background:{}; color:white; padding:2px 8px; border-radius:999px;'
            ' font-size:11px; font-weight:600;">'
            '{}</span>',
            obj.planning_type.color,
            obj.planning_type.name,
        )

    @display(description=_("Resource"))
    def resource_display(self, obj):
        try:
            return str(obj.resource) if obj.resource else f"{obj.resource_content_type} #{obj.resource_object_id}"
        except Exception:
            return f"{obj.resource_content_type} #{obj.resource_object_id}"

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(ResourcePlanningStatus.PLANNED.label): "info",
            _(ResourcePlanningStatus.CONFIRMED.label): "info",
            _(ResourcePlanningStatus.IN_PROGRESS.label): "warning",
            _(ResourcePlanningStatus.COMPLETED.label): "success",
            _(ResourcePlanningStatus.CANCELLED.label): "danger",
        },
    )
    def show_status(self, obj):
        return _(obj.get_status_display())

    @action(description=_("Schedule View"), url_path="schedule-view", icon="calendar_month")
    def schedule_view_action(self, request):
        return redirect(
            reverse("admin:management_resourceplanning_schedule")
        )

    def has_schedule_view_action_permission(self, request):
        return True

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "schedule/",
                self.admin_site.admin_view(
                    ResourcePlanningScheduleView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_schedule",
            ),
        ]
        return custom + urls
