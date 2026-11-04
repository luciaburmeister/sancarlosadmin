from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import AutocompleteSelectFilter
from unfold.decorators import display
from core.admin.company_admin import CompanyRestrictedAdmin
from assets.models import TruckKmSession


class MissingEndKmFilter(admin.SimpleListFilter):
    title = _("End km")
    parameter_name = "missing_end_km"

    def lookups(self, request, model_admin):
        return (("yes", _("Closed without end km")),)

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(end_at__isnull=False, end_km__isnull=True)
        return queryset


@admin.register(TruckKmSession)
class TruckKmSessionAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("truck", "driver", "start_at", "start_km", "end_at", "end_km", "km_driven_display", "end_reason")
    list_filter = (
        ("truck", AutocompleteSelectFilter), ("driver", AutocompleteSelectFilter),
        "start_at", "end_reason", MissingEndKmFilter,
    )
    autocomplete_fields = ["truck", "driver", "company", "attendance", "planning"]
    search_fields = ("truck__name", "truck__identifiers__value", "driver__first_name", "driver__last_name")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "start_at"
    list_select_related = ("truck", "driver")

    @display(description=_("Km driven"))
    def km_driven_display(self, obj):
        return obj.km_driven
