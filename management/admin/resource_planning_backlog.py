from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    MultipleChoicesDropdownFilter,
)
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminColorInputWidget

from core.admin.company_admin import CompanyRestrictedAdmin
from management.forms import InternalLabelsSelectWidget
from management.models import (
    ResourcePlanningBacklogItem,
    ResourcePlanningBacklogLabel,
    ResourcePlanningBacklogStatus,
)
from management.services.resource_planning_backlog import ResourcePlanningBacklogService


class ResourcePlanningBacklogLabelForm(forms.ModelForm):
    class Meta:
        model = ResourcePlanningBacklogLabel
        fields = "__all__"
        widgets = {
            "color": UnfoldAdminColorInputWidget(),
        }


@admin.register(ResourcePlanningBacklogLabel)
class ResourcePlanningBacklogLabelAdmin(ModelAdmin):
    form = ResourcePlanningBacklogLabelForm
    list_display = ["name", "color_preview", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["name"]

    fieldsets = (
        (None, {"fields": ("name", "color", "is_active")}),
    )

    @display(description=_("Color"))
    def color_preview(self, obj):
        return format_html(
            '<span style="display:inline-flex;align-items:center;gap:8px;">'
            '<span style="display:inline-block;width:24px;height:24px;'
            'border-radius:6px;background:{};border:1px solid rgba(0,0,0,.12);"></span>'
            '<code style="font-size:12px;font-family:monospace;">{}</code>'
            '</span>',
            obj.color, obj.color,
        )


@admin.register(ResourcePlanningBacklogItem)
class ResourcePlanningBacklogItemAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = [
        "project_file",
        "type_badge",
        "internal_labels_badges",
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
        ("internal_labels", AutocompleteSelectMultipleFilter),
        ("company", AutocompleteSelectMultipleFilter),
        "is_reusable",
    ]
    list_filter_submit = True
    search_fields = [
        "project_file__name",
        "project_file__reference",
        "notes",
        "internal_labels__name",
    ]
    autocomplete_fields = ["company", "project_file", "planning_type"]
    list_select_related = ["project_file", "planning_type", "company"]
    readonly_fields = ["status", "scheduled_group", "scheduled_at", "created_by"]
    actions = ["cancel_action", "reopen_action"]
    actions_row = ["plan_row_action"]

    class Media(InternalLabelsSelectWidget.Media):
        pass

    fieldsets = (
        (_("Planning"), {"fields": ("company", "project_file", "planning_type")}),
        (_("Estimation"), {
            "fields": ("estimated_days", ("daily_start", "daily_end"), "exclude_weekends", "priority"),
        }),
        (_("Options"), {"fields": ("is_reusable", "show_planning_type", "internal_labels", "notes")}),
        (_("Traceability"), {
            "fields": ("status", "scheduled_group", "scheduled_at", "created_by"),
            "classes": ("collapse",),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("internal_labels")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "internal_labels":
            kwargs["queryset"] = ResourcePlanningBacklogLabel.objects.filter(
                is_active=True,
            ).order_by("name")
            kwargs["widget"] = InternalLabelsSelectWidget()
        form_field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "internal_labels" and form_field is not None:
            widget = InternalLabelsSelectWidget()
            if isinstance(form_field.widget, RelatedFieldWidgetWrapper):
                form_field.widget.widget = widget
            else:
                form_field.widget = widget
        return form_field

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

    @display(description=_("Internal labels"))
    def internal_labels_badges(self, obj):
        labels = list(obj.internal_labels.all())
        if not labels:
            return "—"
        badges = [
            format_html(
                '<span style="display:inline-flex;align-items:center;'
                'background:{};color:white;padding:1px 6px;border-radius:999px;'
                'font-size:10px;font-weight:600;margin-right:3px;">{}</span>',
                label.color, label.name,
            )
            for label in labels
        ]
        return mark_safe("".join(badges))

    @display(
        description=_("Status"),
        ordering="status",
        label={
            ResourcePlanningBacklogStatus.PENDING: "info",
            ResourcePlanningBacklogStatus.SCHEDULED: "success",
            ResourcePlanningBacklogStatus.CANCELLED: "danger",
        },
    )
    def show_status(self, obj):
        return obj.status, obj.get_status_display()

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
