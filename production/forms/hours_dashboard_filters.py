from __future__ import annotations
from django import forms
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget

from core.forms.unfold_date_filters import DateRangeFilterForm


class ProductionHoursDashboardFilterForm(DateRangeFilterForm):
    day = forms.DateField(label=_("Day"), required=False, widget=UnfoldAdminDateWidget)
