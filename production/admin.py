from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from django.utils.translation import gettext_lazy as _
from production.models import (
    ProductionTicket,
    ProductionItem,
)
from django.utils.html import format_html
from django.core.cache import cache


TICKET_STAGE_LOOKUPS_KEY = "production:ticket_filters:stages"
TICKET_STAGE_STATUS_LOOKUPS_KEY = "production:ticket_filters:stage_statuses"
LOOKUPS_CACHE_TIMEOUT = 60



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


class TicketStageFilter(admin.SimpleListFilter):
    title = _("Stage")
    parameter_name = "stage"

    def lookups(self, request, model_admin):
        cached = cache.get(TICKET_STAGE_LOOKUPS_KEY)
        if cached is not None:
            return cached

        stages = set()
        for planning in ProductionItem.objects.exclude(planning={}).values_list("planning", flat=True):
            planning = planning or {}
            stages.update(planning.keys())

        result = [(s, s) for s in sorted(stages)]
        cache.set(TICKET_STAGE_LOOKUPS_KEY, result, LOOKUPS_CACHE_TIMEOUT)
        return result


    def queryset(self, request, queryset):
        stage = self.value()
        if not stage:
            return queryset
        return queryset.filter(items__planning__has_key=stage).distinct()


class TicketStageStatusFilter(admin.SimpleListFilter):
    title = _("Stage status")
    parameter_name = "stage_status"

    def lookups(self, request, model_admin):
        cached = cache.get(TICKET_STAGE_STATUS_LOOKUPS_KEY)
        if cached is not None:
            return cached

        statuses = set()
        for planning in ProductionItem.objects.exclude(planning={}).values_list("planning", flat=True):
            planning = planning or {}
            for info in planning.values():
                if info.get("status"):
                    statuses.add(info["status"])

        result = [(s, s) for s in sorted(statuses)]
        cache.set(TICKET_STAGE_STATUS_LOOKUPS_KEY, result, LOOKUPS_CACHE_TIMEOUT)
        return result


    def queryset(self, request, queryset):
        status = self.value()
        if not status:
            return queryset

        stage = request.GET.get("stage")

        ticket_ids = set()
        for production_item in ProductionItem.objects.filter(ticket__in=queryset).only("ticket_id", "planning"):
            planning = production_item.planning or {}

            if stage:
                info = planning.get(stage, {})
                if info.get("status") == status:
                    ticket_ids.add(production_item.ticket_id)
            else:
                for info in planning.values():
                    if info.get("status") == status:
                        ticket_ids.add(production_item.ticket_id)
                        break

        return queryset.filter(id__in=ticket_ids)


@admin.register(ProductionTicket)
class ProductionTicketAdmin(ModelAdmin):
    list_display = (
        "id",
        "status",
        "created_at",
    )

    list_filter = (
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

    
