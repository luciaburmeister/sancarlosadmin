from django.contrib import admin
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    MultipleChoicesDropdownFilter,
    RangeDateFilter,
)
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminColorInputWidget

from core.admin.company_admin import CompanyRestrictedAdmin
from management.models import (
    ResourcePlanning,
    ResourcePlanningGroup,
    ResourcePlanningStatus,
    ResourcePlanningType,
)
from management.services.resource_planning_backlog import create_backlog_item_from_planning
from management.views.resource_autocomplete import ResourceAutocompleteView
from management.views.resource_planning_bulk import (
    ResourcePlanningAddView,
    ResourcePlanningDeleteDayView,
    ResourcePlanningGroupChangeView,
    ResourcePlanningIndividualChangeView,
    ResourcePlanningMultiAddView,
)
from management.views.resource_planning_schedule import ResourcePlanningScheduleView

PLANNABLE_MODELS = ["employee", "machine", "asset"]


class ResourcePlanningTypeForm(forms.ModelForm):
    class Meta:
        model = ResourcePlanningType
        fields = "__all__"
        widgets = {
            "color": UnfoldAdminColorInputWidget(),
        }


@admin.register(ResourcePlanningType)
class ResourcePlanningTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = ResourcePlanningTypeForm
    list_display = ["name", "color_swatch", "icon", "is_active", "company"]
    list_filter = ["is_active", ("company", AutocompleteSelectMultipleFilter)]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    list_select_related = ["company"]

    fieldsets = (
        (None, {"fields": ("company", "name", "color", "icon", "is_active")}),
    )

    @display(description=_("Color"))
    def color_swatch(self, obj):
        return format_html(
            '<span style="display:inline-flex;align-items:center;gap:8px;">'
            '<span style="display:inline-block;width:24px;height:24px;'
            'border-radius:6px;background:{};border:1px solid rgba(0,0,0,.12);"></span>'
            '<code style="font-size:12px;font-family:monospace;">{}</code>'
            '</span>',
            obj.color, obj.color,
        )


@admin.register(ResourcePlanning)
class ResourcePlanningAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = [
        "type_badge",
        "resource_display",
        "project_file",
        "start",
        "end",
        "show_status",
        "group_link",
        "company",
    ]
    list_filter = [
        ("company", AutocompleteSelectMultipleFilter),
        ("status", MultipleChoicesDropdownFilter),
        ("planning_type", AutocompleteSelectMultipleFilter),
        ("start", RangeDateFilter),
    ]
    search_fields = ["project_file__name", "project_file__reference", "notes"]
    autocomplete_fields = ["company", "project_file", "planning_type"]
    list_select_related = ["project_file", "planning_type", "company", "resource_content_type", "group"]
    actions = ["cancel_group_action", "cancel_individual_action"]
    actions_list = ["schedule_view_action", "multi_add_action", "backlog_action"]

    fieldsets = (
        (_("Planning"), {"fields": ("company", "project_file", "planning_type", "status")}),
        (_("Resource"), {"fields": ("resource_content_type", "resource_object_id")}),
        (_("Schedule"), {"fields": (("start", "end"),)}),
        (_("Notes"), {"fields": ("notes",)}),
    )

    # ── add / change redirects ────────────────────────────────────────────────

    def add_view(self, request, form_url="", extra_context=None):
        return HttpResponseRedirect(reverse("admin:management_resourceplanning_bulk_add"))

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, object_id)
        if obj is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)
        if obj.group_id:
            group = ResourcePlanningGroup.objects.filter(pk=obj.group_id).first()
            if group and not group.is_repetition:
                return HttpResponseRedirect(
                    reverse("admin:management_resourceplanning_group_change", args=[group.pk])
                )
        return HttpResponseRedirect(
            reverse("admin:management_resourceplanning_individual_change", args=[object_id])
        )

    # ── custom URLs ───────────────────────────────────────────────────────────

    def get_urls(self):
        custom = [
            path(
                "schedule/",
                self.admin_site.admin_view(
                    ResourcePlanningScheduleView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_schedule",
            ),
            path(
                "bulk/add/",
                self.admin_site.admin_view(
                    ResourcePlanningAddView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_bulk_add",
            ),
            path(
                "bulk/multi-add/",
                self.admin_site.admin_view(
                    ResourcePlanningMultiAddView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_multi_add",
            ),
            path(
                "group/<int:group_pk>/change/",
                self.admin_site.admin_view(
                    ResourcePlanningGroupChangeView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_group_change",
            ),
            path(
                "resource-autocomplete/",
                self.admin_site.admin_view(ResourceAutocompleteView.as_view()),
                name="management_resourceplanning_resource_autocomplete",
            ),
            path(
                "<int:pk>/change/individual/",
                self.admin_site.admin_view(
                    ResourcePlanningIndividualChangeView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_individual_change",
            ),
            path(
                "<int:pk>/delete-day/",
                self.admin_site.admin_view(
                    ResourcePlanningDeleteDayView.as_view(model_admin=self)
                ),
                name="management_resourceplanning_delete_day",
            ),
            path(
                "<int:pk>/copy-to-backlog/",
                self.admin_site.admin_view(self.copy_to_backlog_view),
                name="management_resourceplanning_copy_to_backlog",
            ),
        ]
        return custom + super().get_urls()

    def copy_to_backlog_view(self, request, pk):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not request.user.has_perm("management.add_resourceplanningbacklogitem"):
            raise PermissionDenied
        planning = self.get_object(request, pk)
        if planning is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, pk)
        create_backlog_item_from_planning(planning, created_by=request.user)
        self.message_user(
            request, _("Planning copied to the backlog as a reusable item.")
        )
        next_url = request.POST.get("next")
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
            return HttpResponseRedirect(next_url)
        return HttpResponseRedirect(
            reverse("admin:management_resourceplanningbacklogitem_changelist")
        )

    # ── formfield overrides ───────────────────────────────────────────────────

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "resource_content_type":
            kwargs["queryset"] = ContentType.objects.filter(model__in=PLANNABLE_MODELS)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_search_results(self, request, queryset, search_term):
        from django.db.models import Q
        from assets.models import Asset
        from company.models import Employee, Machine

        queryset, dupes = super().get_search_results(request, queryset, search_term)
        if search_term:
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
            queryset |= self.model.objects.filter(
                Q(resource_content_type=employee_ct, resource_object_id__in=emp_ids)
                | Q(resource_content_type=machine_ct, resource_object_id__in=machine_ids)
                | Q(resource_content_type=asset_ct, resource_object_id__in=asset_ids)
            )
            dupes = True
        return queryset, dupes

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

    @display(description=_("Resource"))
    def resource_display(self, obj):
        try:
            return str(obj.resource) if obj.resource else f"{obj.resource_content_type} #{obj.resource_object_id}"
        except Exception:
            return f"#{obj.resource_object_id}"

    @display(
        description=_("Status"),
        ordering="status",
        label={
            ResourcePlanningStatus.PLANNED: "info",
            ResourcePlanningStatus.CONFIRMED: "info",
            ResourcePlanningStatus.IN_PROGRESS: "warning",
            ResourcePlanningStatus.COMPLETED: "success",
            ResourcePlanningStatus.CANCELLED: "danger",
        },
    )
    def show_status(self, obj):
        return obj.status, obj.get_status_display()

    @display(description=_("Group"))
    def group_link(self, obj):
        if not obj.group_id:
            return "—"
        url = reverse("admin:management_resourceplanning_group_change", args=[obj.group_id])
        return format_html(
            '<a href="{}" class="text-xs font-mono text-primary-600 hover:underline">#{}</a>',
            url, obj.group_id,
        )

    # ── actions ───────────────────────────────────────────────────────────────

    @admin.action(description=_("Cancel selected (as group)"))
    def cancel_group_action(self, request, queryset):
        groups = ResourcePlanningGroup.objects.filter(
            pk__in=queryset.exclude(group__isnull=True).values_list("group_id", flat=True)
        )
        count = groups.count()
        for group in groups:
            group.cancel_all()
        self.message_user(request, _(f"Cancelled {count} group(s)."))

    @admin.action(description=_("Cancel selected (individually)"))
    def cancel_individual_action(self, request, queryset):
        count = queryset.count()
        for planning in queryset:
            planning.cancel()
        self.message_user(request, _(f"Cancelled {count} planning(s)."))

    @action(description=_("Schedule View"), url_path="schedule-view", icon="calendar_month")
    def schedule_view_action(self, request):
        return HttpResponseRedirect(reverse("admin:management_resourceplanning_schedule"))

    def has_schedule_view_action_permission(self, request):
        return True

    @action(description=_("Multi Planning"), url_path="multi-add", icon="playlist_add")
    def multi_add_action(self, request):
        return HttpResponseRedirect(reverse("admin:management_resourceplanning_multi_add"))

    def has_multi_add_action_permission(self, request):
        return request.user.has_perm("management.add_resourceplanning")

    @action(description=_("Backlog"), url_path="backlog", icon="pending_actions")
    def backlog_action(self, request):
        return HttpResponseRedirect(
            reverse("admin:management_resourceplanningbacklogitem_changelist")
        )

    def has_backlog_action_permission(self, request):
        return request.user.has_perm("management.view_resourceplanningbacklogitem")
