from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import DropdownFilter, RadioFilter

from sales.models import QuoteProductionStatus, QuoteStatus


class HasProductionFilter(RadioFilter):
    title = _("includes production")
    parameter_name = "has_production"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.exclude(
                production_status=QuoteProductionStatus.NO_PRODUCTION
            )
        if self.value() == "no":
            return queryset.filter(
                production_status=QuoteProductionStatus.NO_PRODUCTION
            )
        return queryset


class ProductionSchedulingFilter(DropdownFilter):
    title = _("production scheduling")
    parameter_name = "production_scheduling"

    def lookups(self, request, model_admin):
        return (
            ("not_converted", _("Production – no ticket")),
            ("unscheduled", _("Unscheduled")),
            ("scheduled", _("Scheduled")),
            ("needs_attention", _("Accepted – missing schedule")),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "not_converted":
            return queryset.filter(
                production_status=QuoteProductionStatus.NOT_CONVERTED
            )
        if value == "unscheduled":
            return queryset.filter(
                production_status__in=[
                    QuoteProductionStatus.NOT_CONVERTED,
                    QuoteProductionStatus.UNSCHEDULED,
                ]
            )
        if value == "scheduled":
            return queryset.filter(
                production_status=QuoteProductionStatus.SCHEDULED
            )
        if value == "needs_attention":
            return queryset.filter(
                status=QuoteStatus.ACCEPTED,
                production_status__in=[
                    QuoteProductionStatus.NOT_CONVERTED,
                    QuoteProductionStatus.UNSCHEDULED,
                ],
            )
        return queryset
