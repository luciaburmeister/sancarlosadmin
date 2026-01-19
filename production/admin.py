from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from django.utils.translation import gettext_lazy as _
from production.models import (
    ProductionTicket,
    ProductionItem,
    ProductionStageItem
)
from django.utils.html import format_html
from production.filters.ticket_filters import TicketStageFilter, TicketStageStatusFilter

from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST


class ProductionItemInline(TabularInline):
    model = ProductionItem
    extra = 0
    show_change_link = True
    tab=True

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

        STATUS_COLORS = {
            "pending": "#9ca3af",       # gray
            "in_progress": "#f59e0b",   # amber
            "done": "#10b981",          # green
            "cancelled": "#ef4444",     # red
        }

        rows = []
        for stage, info in obj.planning.items():
            status = info.get("status", "-")
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
    list_display = (
        "id",
        "status",
        "created_at",
    )

    list_filter = (
        TicketStageFilter,
        TicketStageStatusFilter,
        "status",
        "content_type",
    )

    search_fields = (
        "object_id",
    )

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
                    "context"
                )
            },
        ),

    )   

from django.contrib import admin, messages
from django.urls import path, reverse
from django.shortcuts import get_object_or_404, redirect


@admin.register(ProductionStageItem)
class ProductionStageItemAdmin(ModelAdmin):
    change_form_template = "admin/production/productionstageitem/change_form.html"

    list_display = ("id", "ticket_id", "component_display", "status")

    readonly_fields = ("ticket", "component_display", "status")
    fieldsets = ((None, {"fields": (("ticket", "status"), "component_display")}),)

    def component_display(self, obj):
        data = obj.data or {}
        return data.get("component") or data.get("component_id") or "-"
    component_display.short_description = _("Component")


    def get_urls(self):
        urls = super().get_urls()

        custom = [
            path(
                "<int:object_id>/stages/<str:stage>/start/",
                self.admin_site.admin_view(self.stage_start),
                name="production_productionstageitem_stage_start",
            ),
            path(
                "<int:object_id>/stages/<str:stage>/finish/",
                self.admin_site.admin_view(self.stage_finish),
                name="production_productionstageitem_stage_finish",
            ),
        ]
        return custom + urls

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}

        obj = None
        if object_id:
            obj = self.get_object(request, object_id)

        planning = (obj.planning or {}) if obj else {}

        stages = []
        for name, info in planning.items():
            info = info or {}
            stages.append(
                {
                    "name": name,
                    "status": info.get("status") or "pending",
                    "started_at": info.get("started_at"),
                    "completed_at": info.get("completed_at"),
                }
            )

        extra_context["planning_stages"] = stages
        return super().changeform_view(request, object_id, form_url, extra_context)

    def stage_start(self, request, object_id, stage):
        if request.method != "POST":
            return redirect(reverse("admin:production_productionstageitem_change", args=[object_id]))

        obj = self.get_object(request, object_id)
        planning = obj.planning or {}
        info = planning.get(stage, {}) or {}

        info["status"] = "in_progress"
        info["started_at"] = info.get("started_at") or timezone.now().isoformat()
        info["completed_at"] = None

        planning[stage] = info
        obj.planning = planning
        obj.save(update_fields=["planning", "updated_at"])

        return redirect(reverse("admin:production_productionstageitem_change", args=[object_id]))

    def stage_finish(self, request, object_id, stage):
        if request.method != "POST":
            return redirect(reverse("admin:production_productionstageitem_change", args=[object_id]))

        obj = self.get_object(request, object_id)
        planning = obj.planning or {}
        info = planning.get(stage, {}) or {}

        info["status"] = "done"
        info["started_at"] = info.get("started_at") or timezone.now().isoformat()
        info["completed_at"] = timezone.now().isoformat()

        planning[stage] = info
        obj.planning = planning
        obj.save(update_fields=["planning"])

        return redirect(reverse("admin:production_productionstageitem_change", args=[object_id]))