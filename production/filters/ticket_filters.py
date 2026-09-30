import unicodedata

from django.utils.translation import gettext_lazy as _
from django.core.cache import cache

from unfold.contrib.filters.admin import MultipleDropdownFilter

from company.models import Employee
from production.models import ProductionItem, ProductionTicket
from production.constants import (
    TICKET_STAGE_LOOKUPS_KEY,
    TICKET_STAGE_STATUS_LOOKUPS_KEY,
    LOOKUPS_CACHE_TIMEOUT,
)


def selected_values(value):
    """Normalize a multiple-dropdown value into a clean list of strings.

    The filter value is a list when the parameter is present in the query
    string, ``None`` when it is absent, and a plain string while admin facet
    counts are being computed.
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    return [v for v in value if v not in (None, "")]


class WorkerStageLookupsMixin:
    """Limit stage choices to a shop-floor worker's assigned stages."""

    def lookups(self, request, model_admin):
        choices = super().lookups(request, model_admin)
        worker = Employee.stage_restricted_worker(request.user)
        if worker is None:
            return choices
        visible = worker.visible_production_stages(worker.company_id)
        return [
            (value, label) for value, label in choices
            if unicodedata.normalize("NFC", value) in visible
        ]


class TicketStageFilter(WorkerStageLookupsMixin, MultipleDropdownFilter):
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
        stages = selected_values(self.value())
        if not stages:
            return queryset
        return queryset.filter(items__planning__has_any_keys=stages).distinct()


class TicketIncompleteStageFilter(WorkerStageLookupsMixin, MultipleDropdownFilter):
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
        values = selected_values(self.value())
        if not values:
            return queryset

        matching_ids = []
        queryset = queryset.prefetch_related("items")

        for ticket in queryset:
            if any(ticket.has_incomplete_stage(value) for value in values):
                matching_ids.append(ticket.pk)

        return queryset.filter(pk__in=matching_ids)


class TicketStageStatusFilter(MultipleDropdownFilter):
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
        statuses = selected_values(self.value())
        if not statuses:
            return queryset

        stages = [stage for stage in request.GET.getlist("stage") if stage]

        ticket_ids = set()
        for production_item in ProductionItem.objects.filter(ticket__in=queryset).only("ticket_id", "planning"):
            planning = production_item.planning or {}

            if stages:
                infos = [planning.get(stage) or {} for stage in stages]
            else:
                infos = planning.values()

            for info in infos:
                if info.get("status") in statuses:
                    ticket_ids.add(production_item.ticket_id)
                    break

        return queryset.filter(id__in=ticket_ids)


class TicketLastStageLabelFilter(WorkerStageLookupsMixin, MultipleDropdownFilter):
    title = _("Last Stage")
    parameter_name = "last_stage_label"

    def lookups(self, request, model_admin):
        labels = (
            ProductionTicket.objects.exclude(last_stage_label__isnull=True)
            .exclude(last_stage_label="")
            .values_list("last_stage_label", flat=True)
            .distinct()
            .order_by("last_stage_label")
        )
        return [(label, label) for label in labels]

    def queryset(self, request, queryset):
        labels = selected_values(self.value())
        if not labels:
            return queryset
        return queryset.filter(last_stage_label__in=labels)
