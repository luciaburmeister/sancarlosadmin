from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import DropdownFilter, TextFilter

from management.models import ProjectFile


class ProjectFileInvoicingFilter(DropdownFilter):
    title = _("Invoicing")
    parameter_name = "invoicing"

    NOT_INVOICED = "not_invoiced"
    PARTIALLY_INVOICED = "partially_invoiced"
    FULLY_INVOICED = "fully_invoiced"

    def lookups(self, request, model_admin):
        return (
            (self.NOT_INVOICED, _("Not invoiced")),
            (self.PARTIALLY_INVOICED, _("Partially invoiced")),
            (self.FULLY_INVOICED, _("Fully invoiced")),
        )

    def queryset(self, request, queryset):
        # Same rule as ProjectFile.is_fully_invoiced, on the cached totals.
        fully = Q(
            total_amount__gt=0,
            percentage_invoiced__gte=ProjectFile.FULLY_INVOICED_PERCENTAGE,
            advance_pending_amount__lte=ProjectFile.ADVANCE_PENDING_TOLERANCE,
        )
        value = self.value()
        if value == self.NOT_INVOICED:
            return queryset.filter(total_invoiced=0)
        if value == self.PARTIALLY_INVOICED:
            return queryset.filter(total_invoiced__gt=0).exclude(fully)
        if value == self.FULLY_INVOICED:
            return queryset.filter(fully)
        return queryset


class ProjectFileCreatedByFilter(TextFilter):
    """Users must not be listed, so the creator's exact username is typed in
    instead of being picked from a list."""

    title = _("Creator")
    parameter_name = "created_by"

    def queryset(self, request, queryset):
        username = (self.value() or "").strip()
        if not username:
            return queryset
        return queryset.filter(created_by__username__iexact=username)
