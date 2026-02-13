from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import get_object_or_404

from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.decorators import display

from production.models import (
    ProductionTicket,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionResource,
    OperationRequirement,
)
from production.filters.ticket_filters import TicketStageFilter, TicketStageStatusFilter
from production.views.production_item_stages_view import ProductionItemAdvanceStageView
from production.views.production_ticket_stages_view import ProductionTicketStagesView
from configurator.models import ProductTemplateComponentGroup

from production.components.status_badge import StatusBadge


class ProductionItemInline(TabularInline):
    model = ProductionItem
    extra = 0
    show_change_link = True
    tab = True

    fields = (
        "status",
        "planning_display",
        "data",
    )
    readonly_fields = ("planning_display",)

    @admin.display(description=_("Production planning"))
    def planning_display(self, obj):
        if not obj.planning:
            return "-"

        STATUS_COLORS = StatusBadge.STATUS_COLORS

        rows = []
        for stage, info in (obj.planning or {}).items():
            status = (info or {}).get("status", "-")
            color = STATUS_COLORS.get(status, "#6b7280")

            rows.append(
                f"""
                <tr>
                    <td style="padding:6px 8px; font-weight:600;">
                        {stage}
                    </td>
                    <td style="padding:6px 8px;">
                        <span style="
                            display:inline-block;
                            padding:2px 8px;
                            border-radius:12px;
                            font-size:12px;
                            background-color: {color} !important;
                            color:white;
                            text-transform:capitalize;
                        ">
                            {_(status.replace("_", " "))}
                        </span>
                    </td>
                </tr>
                """
            )

        return format_html(
            """
            <div style="
                border:1px solid #e5e7eb;
                border-radius:6px;
                overflow:hidden;
                background:#fff;
            ">
                <table style="
                    width:100%;
                    border-collapse:collapse;
                    font-size:13px;
                ">
                    <thead style="background:#f9fafb;">
                        <tr>
                            <th style="padding:6px 8px; text-align:left;">Stage</th>
                            <th style="padding:6px 8px; text-align:left;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {}
                    </tbody>
                </table>
            </div>
            """,
            format_html("".join(rows)),
        )


@admin.register(ProductionTicket)
class ProductionTicketAdmin(ModelAdmin):
    change_form_template = "admin/production/productionticket/change_form.html"

    list_display = ("id", "status", "created_at")

    list_filter = (
        TicketStageFilter,
        TicketStageStatusFilter,
        "status",
        "content_type",
    )

    search_fields = ("object_id",)

    inlines = [ProductionItemInline]

    readonly_fields = (
        "content_type",
        "object_id",
        "source_object",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "status",
                    ("content_type", "object_id"),
                    "source_object",
                    "context",
                )
            },
        ),
    )

    def get_urls(self):
        urls = super().get_urls()

        stages_view = self.admin_site.admin_view(
            ProductionTicketStagesView.as_view(model_admin=self)
        )
        advance_view = self.admin_site.admin_view(
            ProductionItemAdvanceStageView.as_view(model_admin=self)
        )

        custom = [
            path(
                "<int:object_id>/stages/",
                stages_view,
                name="production_productionticket_stages",
            ),
            path(
                "<int:ticket_id>/stages/<str:stage_name>/action/",
                advance_view,
                name="production_productionticket_stage_action",
            ),
        ]
        return custom + urls


class ProductionItemSessionInline(StackedInline):
    model = ProductionItemSession
    extra = 0
    show_change_link = True
    tab = True

    fields = (
        "session_type_display",
        "created_by",
        "completed_by",
        "production_stage",
        "status",
        "start_time",
        "end_time",
        "duration_display",
        "comments",
    )

    readonly_fields = (
        "session_type_display",
        "duration_display",
    )

    ordering = ("-start_time",)

    def duration_display(self, obj):
        if not obj.duration:
            return "—"
        total_seconds = int(obj.duration.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m"

    duration_display.short_description = _("Duration")

    @display(
        description=_("Type"),
        label={
            "activityfinished": "success",
            "session_finished": "info",
            "working": "warning",
        },
    )
    def session_type_display(self, obj):
        if obj.kind:
            return obj.get_kind_display()

        if obj.status == ProductionItemSessionStatus.IN_PROGRESS:
            return "working"

        return None


class OperationRequirementInline(TabularInline):
    model = OperationRequirement
    extra = 0


@admin.register(ProductionItem)
class ProductionItemAdmin(ModelAdmin):
    list_display = (
        "id",
        "ticket",
        "status",
        "sessions_count",
        "created_at",
    )

    list_filter = ("status",)
    search_fields = ("id", "ticket__id")

    readonly_fields = (
        "component",
        "component_id",
        "quantity",
        "length",
        "data",
        "planning",
    )

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "ticket",
                    "status",
                    ("component", "component_id"),
                    ("quantity", "length"),
                    "data",
                    "planning",
                )
            },
        ),
    )

    inlines = [ProductionItemSessionInline, OperationRequirementInline]

    def sessions_count(self, obj):
        return obj.sessions.count()

    sessions_count.short_description = _("Sessions")


@admin.register(ProductionItemSession)
class ProductionItemSessionAdmin(ModelAdmin):
    list_display = (
        "id",
        "production_item",
        "production_stage",
        "show_status_colored",
        "start_time",
        "end_time",
    )

    list_filter = (
        "status",
        "production_stage",
    )

    search_fields = (
        "production_stage",
        "created_by__name",
        "completed_by__name",
        "production_item__id",
    )

    autocomplete_fields = (
        "production_item",
        "created_by",
        "completed_by",
    )

    actions = (
        "start_sessions",
        "finish_sessions",
    )

    ordering = ("-created_at",)

    @display(
        description=_("Status"),
        ordering="status",
        label={
            ProductionItemSessionStatus.DONE: "success",
            ProductionItemSessionStatus.PENDING: "info",
            ProductionItemSessionStatus.IN_PROGRESS: "warning",
            ProductionItemSessionStatus.CANCELLED: "danger",
        },
    )
    def show_status_colored(self, obj):
        return obj.status


@admin.register(ProductionResource)
class ProductionResourceAdmin(ModelAdmin):
    list_display = ("id", "target", "daily_capacity_minutes", "is_active")
    list_filter = ("is_active",)
    search_fields = ("object_id",)
    readonly_fields = ("target",)
    autocomplete_fields = ("company",)

    fieldsets = (
        (None, {
            "fields": (
                ("content_type", "object_id"),
                "daily_capacity_minutes",
                ("company", "is_active"),
            )
        }),
    )

    def target(self, obj):
        return obj.target


class PlannedSessionResourceInline(TabularInline):
    model = PlannedSessionResource
    extra = 0
    autocomplete_fields = ("resource",)


@admin.register(PlannedProductionSession)
class PlannedProductionSessionAdmin(ModelAdmin):
    list_display = (
        "id",
        "production_item",
        "production_stage",
        "planned_start",
        "planned_end",
        "primary_resource",
        "locked",
        "source",
    )
    list_filter = ("locked", "source")
    search_fields = ("production_stage",)
    autocomplete_fields = ("production_item", "primary_resource")
    inlines = (PlannedSessionResourceInline,)

    fieldsets = (
        (None, {
            "fields": (
                "production_item",
                "production_stage",
            )
        }),
        ("Schedule", {
            "fields": (
                ("planned_start", "planned_end"),
                "primary_resource",
            )
        }),
        ("Controls", {
            "fields": (
                "locked",
                "source",
                "notes",
            )
        }),
    )