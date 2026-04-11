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

class ItemsMissingAccountFilter(RadioFilter):
    title = _("items missing account")
    parameter_name = "items_missing_account"

    def lookups(self, request, model_admin):
        return (
            ("yes", _("Yes")),
            ("no", _("No")),
        )

    def queryset(self, request, queryset):
        if self.value() == "yes":
            # Invoices that have items where at least one item has account=None
            return queryset.filter(items__account__isnull=True, items__isnull=False).distinct()
        if self.value() == "no":
            # Invoices where all items have an account
            # Exclude invoices that have any item with no account
            return queryset.exclude(items__account__isnull=True, items__isnull=False)
        return queryset
