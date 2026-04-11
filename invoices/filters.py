from django.utils.translation import gettext_lazy as _
from unfold.contrib.filters.admin import RadioFilter

class HasItemsFilter(RadioFilter):
    title = _("has items")
    parameter_name = "has_items"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(items__isnull=False).distinct()
        if self.value() == "no":
            return queryset.filter(items__isnull=True)
        return queryset
