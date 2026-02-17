from django import forms
from unfold.widgets import UnfoldAdminDateWidget

class EmployeeSessionsFilterForm(forms.Form):
    from_date = forms.DateField(
        required=False,
        widget=UnfoldAdminDateWidget
    )
    to_date = forms.DateField(
        required=False,
        widget=UnfoldAdminDateWidget
    )

    class Media:
        js = [
            f"admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]
