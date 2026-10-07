from __future__ import annotations

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminSelectWidget

from core.forms.unfold_date_filters import UnfoldDateWidgetMediaMixin


GROUP_MACHINE_PEOPLE = "machine_people"
GROUP_MACHINE = "machine"
GROUP_EMPLOYEE = "employee"
GROUP_TICKET = "ticket"
PERIOD_DAY = "day"
PERIOD_WEEK = "week"

GROUPING_CHOICES = (
    (GROUP_MACHINE_PEOPLE, _("All")),
    (GROUP_MACHINE, _("Machine")),
    (GROUP_EMPLOYEE, _("Operator")),
    (GROUP_TICKET, _("Production order")),
)


class ProductionScheduleFilterForm(UnfoldDateWidgetMediaMixin, forms.Form):
    period = forms.ChoiceField(
        choices=((PERIOD_DAY, _("Day")), (PERIOD_WEEK, _("Week"))),
        required=False,
        initial=PERIOD_DAY,
    )

    day = forms.DateField(
        label=_("Day"),
        required=False,
        initial=timezone.localdate,
        widget=UnfoldAdminDateWidget,
    )

    group_by = forms.ChoiceField(
        label=_("Group by"),
        choices=GROUPING_CHOICES,
        initial=GROUP_MACHINE_PEOPLE,
        widget=UnfoldAdminSelectWidget,
    )
