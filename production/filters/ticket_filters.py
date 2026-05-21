from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from django.core.cache import cache

from production.models import ProductionItem, ProductionTicketStatus
from production.constants import (
    TICKET_STAGE_LOOKUPS_KEY,
    TICKET_STAGE_STATUS_LOOKUPS_KEY,
    LOOKUPS_CACHE_TIMEOUT,
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

        # Attempt to sort using company stages if available
        company = getattr(request.user, "company", None)
        if not company and hasattr(model_admin, "get_queryset"):
            # Try to get company from first object if possible, or just use request if it was set by middleware
            pass
        company_stages = []
        if hasattr(request, "company") and request.company:
            company_stages = list(request.company.production_stages or [])
        
        def sort_key(s):
            try:
                return (0, company_stages.index(s))
            except ValueError:
                return (1, s)

        result = [(s, s) for s in sorted(stages, key=sort_key)]
        cache.set(TICKET_STAGE_LOOKUPS_KEY, result, LOOKUPS_CACHE_TIMEOUT)
        return result


    def queryset(self, request, queryset):
        stage = self.value()
        if not stage:
            return queryset
        return queryset.filter(items__planning__has_key=stage).distinct()
    
class TicketIncompleteStageFilter(admin.SimpleListFilter):
    title = _("Incomplete stage")
    parameter_name = "incomplete_stage"

    def lookups(self, request, model_admin):
        stages = set()

        queryset = model_admin.get_queryset(request).prefetch_related("items")
        for ticket in queryset:
            for stage in ticket.get_stage_order():
                stages.add(stage)

        return [(stage, stage) for stage in sorted(stages)]

    def queryset(self, request, queryset):
        value = self.value()
        if not value:
            return queryset

        matching_ids = []
        queryset = queryset.prefetch_related("items")

        for ticket in queryset:
            if ticket.has_incomplete_stage(value):
                matching_ids.append(ticket.pk)

        return queryset.filter(pk__in=matching_ids)

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