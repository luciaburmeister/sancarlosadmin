from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    MultipleChoicesDropdownFilter,
)
from unfold.decorators import action, display

from core.admin.company_admin import CompanyRestrictedAdmin
from management.models import (
    ResourcePlanningBacklogItem,
    ResourcePlanningBacklogStatus,
)
from management.services.resource_planning_backlog import ResourcePlanningBacklogService


@admin.register(ResourcePlanningBacklogItem)
class ResourcePlanningBacklogItemAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = [
        "project_file",
        "type_badge",
        "estimated_days",
        "priority",
        "is_reusable",
        "show_status",
        "created_at",
        "company",
    ]
    list_filter = [
        ("status", MultipleChoicesDropdownFilter),
        ("planning_type", AutocompleteSelectMultipleFilter),
        ("company", AutocompleteSelectMultipleFilter),
        "is_reusable",
    ]
    search_fields = ["project_file__name", "project_file__reference", "notes"]
    autocomplete_fields = ["company", "project_file", "planning_type"]
    list_select_related = ["project_file", "planning_type", "company"]
    readonly_fields = ["status", "scheduled_group", "scheduled_at", "created_by"]
    actions = ["cancel_action", "reopen_action"]
    actions_row = ["plan_row_action"]

    fieldsets = (
        (_("Planning"), {"fields": ("company", "project_file", "planning_type")}),
        (_("Estimation"), {
            "fields": ("estimated_days", ("daily_start", "daily_end"), "exclude_weekends", "priority"),
        }),
        (_("Options"), {"fields": ("is_reusable", "show_planning_type", "notes")}),
        (_("Traceability"), {
            "fields": ("status", "scheduled_group", "scheduled_at", "created_by"),
            "classes": ("collapse",),
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    # ── list display ──────────────────────────────────────────────────────────

    @display(description=_("Type"))
    def type_badge(self, obj):
        if not obj.planning_type_id:
            return "—"
        pt = obj.planning_type
        return format_html(
            '<span style="display:inline-flex;align-items:center;gap:4px;'
            'background:{};color:white;padding:2px 8px;border-radius:999px;'
            'font-size:11px;font-weight:600;">{}</span>',
            pt.color, pt.name,
        )

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _(ResourcePlanningBacklogStatus.PENDING.label): "info",
            _(ResourcePlanningBacklogStatus.SCHEDULED.label): "success",
            _(ResourcePlanningBacklogStatus.CANCELLED.label): "danger",
        },
    )
    def show_status(self, obj):
        return _(obj.get_status_display())

    # ── actions ───────────────────────────────────────────────────────────────

    @action(description=_("Plan"), url_path="plan", icon="event_available")
    def plan_row_action(self, request, object_id):
        item = self.get_object(request, object_id)
        if item is None or item.status != ResourcePlanningBacklogStatus.PENDING:
            self.message_user(
                request,
                _("Only pending backlog items can be planned."),
                level="warning",
            )
            return HttpResponseRedirect(
                reverse("admin:management_resourceplanningbacklogitem_changelist")
            )
        return HttpResponseRedirect(
            reverse("admin:management_resourceplanning_bulk_add")
            + f"?backlog_item={object_id}"
        )

    def has_plan_row_action_permission(self, request, object_id=None):
        return request.user.has_perm("management.add_resourceplanning")

    @admin.action(description=_("Cancel selected backlog items"))
    def cancel_action(self, request, queryset):
        count = queryset.filter(status=ResourcePlanningBacklogStatus.PENDING).update(
            status=ResourcePlanningBacklogStatus.CANCELLED
        )
        self.message_user(request, _(f"Cancelled {count} backlog item(s)."))

    @admin.action(description=_("Reopen selected backlog items"))
    def reopen_action(self, request, queryset):
        count = 0
        for item in queryset.exclude(status=ResourcePlanningBacklogStatus.PENDING):
            ResourcePlanningBacklogService.reopen(item)
            count += 1
        self.message_user(request, _(f"Reopened {count} backlog item(s)."))
