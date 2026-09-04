from __future__ import annotations
from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget


class UnfoldDateWidgetMediaMixin:
    """Shared Media for forms using Unfold's date/time admin widgets outside a
    ModelAdmin change form. Django's admin only auto-includes the JS the
    calendar-icon popup needs (vendor jQuery, calendar.js, DateTimeShortcuts.js,
    core.js) on change_form/change_list — a custom dashboard template needs it
    added explicitly via `{{ form.media }}`, or the picker silently renders as
    a bare text input with no calendar button.
    """

    class Media:
        # core.js must precede calendar.js/DateTimeShortcuts.js — matches the
        # order UnfoldAdminDateWidget.Media declares them, so Django's Media
        # merge doesn't raise a MediaOrderConflictWarning.
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/core.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
        ]


class DateRangeFilterForm(UnfoldDateWidgetMediaMixin, forms.Form):
    """Base for admin-dashboard GET filter forms with a date_from/date_to range.

    Subclasses add their own extra fields (e.g. company, employee, granularity).
    """

    date_from = forms.DateField(label=_("From"), required=False, widget=UnfoldAdminDateWidget)
    date_to = forms.DateField(label=_("To"), required=False, widget=UnfoldAdminDateWidget)

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            cleaned_data["date_from"], cleaned_data["date_to"] = date_to, date_from
        return cleaned_data
