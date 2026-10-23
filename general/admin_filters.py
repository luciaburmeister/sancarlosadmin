"""Changelist filters any app's admin can use."""
from django.core.exceptions import ValidationError
from django.core.validators import EMPTY_VALUES
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.utils import parse_date_str


class RangeDayFilter(RangeDateFilter):
    """
    Unfold's date range for a DateTimeField. The plain RangeDateFilter
    compares the datetime with a date, i.e. with midnight, so everything
    on the "to" day is left out; this one compares the (local) day.
    """

    def queryset(self, request, queryset):
        filters = {}
        for suffix, lookup in (("_from", "gte"), ("_to", "lte")):
            value = self.used_parameters.get(f"{self.parameter_name}{suffix}")
            if value not in EMPTY_VALUES and isinstance(value, str):
                filters[f"{self.parameter_name}__date__{lookup}"] = parse_date_str(value)
        try:
            return queryset.filter(**filters)
        except (ValueError, ValidationError):
            return None
