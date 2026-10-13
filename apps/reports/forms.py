from django import forms
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
)

from apps.reports.constants import ReportVisibility
from apps.reports.spec import ReportSpec
from core.forms.select import StaticSelect2MultipleWidget
from core.forms.unfold_date_filters import UnfoldDateWidgetMediaMixin


class ReportCreateForm(forms.Form):
    def __init__(self, *args, companies=None, **kwargs):
        super().__init__(*args, **kwargs)
        if companies is not None and companies.count() > 1:
            self.fields["companies"] = forms.ModelMultipleChoiceField(
                label=_("Companies"),
                queryset=companies,
                widget=StaticSelect2MultipleWidget,
                required=True,
                help_text=_("Reports include every company you select here."),
            )

    prompt = forms.CharField(
        label=_("Describe the report"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 5}),
        help_text=_(
            "Example: Total sales invoices by client for the last 90 days as a bar chart."
        ),
    )
    visibility = forms.ChoiceField(
        label=_("Visibility"),
        choices=ReportVisibility.choices,
        initial=ReportVisibility.PRIVATE,
        widget=UnfoldAdminSelectWidget,
    )


_DATE_LABELS = {
    "date_from": _("From"),
    "date_to": pgettext_lazy("date range", "To"),
}


def build_parameter_form(spec: ReportSpec, data=None):
    """Dynamic form for parameter slots; date fields use UnfoldAdminDateWidget."""

    initial = {}
    for filt in spec.filters:
        if filt.parameter and filt.value not in (None, ""):
            initial[filt.parameter] = filt.value

    fields = {}
    has_dates = False
    for name in spec.parameters:
        if "date" in name.lower():
            has_dates = True
            fields[name] = forms.DateField(
                label=_DATE_LABELS.get(name, name.replace("_", " ").title()),
                required=False,
                widget=UnfoldAdminDateWidget,
            )
        else:
            fields[name] = forms.CharField(
                label=name.replace("_", " ").title(),
                required=False,
                widget=UnfoldAdminTextInputWidget,
            )

    bases = (UnfoldDateWidgetMediaMixin, forms.Form) if has_dates else (forms.Form,)
    form_class = type("ReportParameterForm", bases, fields)
    if data is not None:
        return form_class(data)
    return form_class(initial=initial)
