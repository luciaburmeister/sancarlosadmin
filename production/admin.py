from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.utils.html import format_html
from django.urls import path, reverse
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from unfold.admin import ModelAdmin, TabularInline, StackedInline
from unfold.decorators import display
from unfold.views import UnfoldModelAdminViewMixin

from production.models import (
    ProductionTicket,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionResource,
    OperationRequirement,
    ProductionItemStatus,
)
from production.filters.ticket_filters import TicketStageFilter, TicketStageStatusFilter
from production.views.production_stages_view import ProductionItemAdvanceStageView
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


class ProductionTicketStagesView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Production stages")
    permission_required = ("production.view_productionticket",)
    template_name = "admin/production/productionticket/planning.html"

    def _status_display(self, status_val: str):
        status_val = (status_val or "").strip()
        color = StatusBadge.STATUS_COLORS.get(status_val, "#6b7280")
        label = _(status_val.replace("_", " ")) if status_val else "—"
        return color, label


    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        ticket = get_object_or_404(ProductionTicket, pk=self.kwargs["object_id"])
        ctx["ticket"] = ticket

        config = ticket.source_object
        product = getattr(config, "product", None)
        stage_order = list(getattr(product, "production_stages", []) or [])

        items = list(ticket.items.select_related("component").order_by("id"))
        employee = getattr(self.request.user, "employee_profile", None)

        ticket_current_stage = None

        for sname in stage_order:
            if any(
                (it.planning or {}).get(sname, {}).get("status") == ProductionItemStatus.IN_PROGRESS
                for it in items
            ):
                ticket_current_stage = sname
                break

        if ticket_current_stage is None:
            for sname in stage_order:
                if any(
                    ((it.planning or {}).get(sname, {}).get("status") or ProductionItemStatus.PENDING)
                    != ProductionItemStatus.DONE
                    for it in items
                ):
                    ticket_current_stage = sname
                    break

        if ticket_current_stage is None and stage_order:
            ticket_current_stage = stage_order[0]

        stages_ui = []

        for stage_name in stage_order:
            grouped_item_ids = set()

            if product:
                group = (
                    ProductTemplateComponentGroup.objects
                    .filter(product_template=product, stage=stage_name)
                    .prefetch_related("components")
                    .first()
                )
                if group:
                    component_ids = set(group.components.values_list("id", flat=True))
                    grouped_item_ids = {
                        item.id for item in items
                        if item.component_id in component_ids
                    }

            units = []

            if grouped_item_ids:
                unit_items = [item for item in items if item.id in grouped_item_ids]
                unit = self._build_unit(stage_name, unit_items, employee)
                if unit["relevant"]:
                    units.append(unit)

            for item in items:
                if item.id in grouped_item_ids:
                    continue
                unit = self._build_unit(stage_name, [item], employee)
                if unit["relevant"]:
                    units.append(unit)

            if not units:
                continue

            is_current_stage = (stage_name == ticket_current_stage)
            for u in units:
                u["is_current"] = is_current_stage

            stages_ui.append({
                "stage_name": stage_name,
                "units": units,
                "action_url": reverse(
                    "admin:production_productionticket_stage_action",
                    args=[ticket.pk, stage_name],
                ),
            })

        ctx["stages_ui"] = stages_ui
        return ctx

    def _build_unit(self, stage_name, items, employee):
        infos = []

        for item in items:
            planning = item.planning or {}
            info = planning.get(stage_name) or {}
            status = info.get("status") or ProductionItemStatus.PENDING
            infos.append((item, info, status))

        statuses = [status for _, _, status in infos]

        if all(s == ProductionItemStatus.DONE for s in statuses):
            agg_status = ProductionItemStatus.DONE
        elif any(s == ProductionItemStatus.IN_PROGRESS for s in statuses):
            agg_status = ProductionItemStatus.IN_PROGRESS
        else:
            agg_status = ProductionItemStatus.PENDING

        status_color, status_label = self._status_display(agg_status)

        started_at = min(
            (info.get("started_at") for _, info, _ in infos if info.get("started_at")),
            default=None,
        )
        completed_at = max(
            (info.get("completed_at") for _, info, _ in infos if info.get("completed_at")),
            default=None,
        )

        has_active_session = False
        if employee is not None:
            has_active_session = ProductionItemSession.objects.filter(
                production_item__in=[item for item, _, _ in infos],
                production_stage=stage_name,
                created_by=employee,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                end_time__isnull=True,
            ).exists()

        relevant = any(status != ProductionItemStatus.DONE for status in statuses)

        return {
            "items": [
                {
                    "id": item.id,
                    "label": (item.component.name if item.component else (item.data.get("component") or "—")),
                }
                for item, _, _ in infos
            ],
            "item_ids": ",".join(str(item.id) for item, _, _ in infos),
            "status": agg_status,
            "status_color": status_color,
            "status_label": status_label,
            "started_at": started_at,
            "completed_at": completed_at,
            "has_active_session": has_active_session,
            "is_current": False,
            "relevant": relevant,
        }


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
            "activity finished": "success",
            "session finished": "info",
            "working": "warning",
        },
    )
    def session_type_display(self, obj):
        comments = (obj.comments or "").lower()

        if "activity finished" in comments:
            return "Activity finished"
        if "session finished" in comments:
            return "Session finished"
        if obj.status == ProductionItemSessionStatus.IN_PROGRESS:
            return "Working"
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