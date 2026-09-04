from __future__ import annotations
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminSelectWidget

from core.forms.unfold_date_filters import UnfoldDateWidgetMediaMixin
from production.models import ProductionResource

class ScheduleFilterForm(UnfoldDateWidgetMediaMixin, forms.Form):
    day = forms.DateField(
        label=_("Day"),
        required=True,
        initial=timezone.localdate,
        widget=UnfoldAdminDateWidget,
    )

    resource_id = forms.ModelChoiceField(
        label=_("Production resource"),
        required=False,
        queryset=ProductionResource.objects.none(),
        empty_label=_("All resources"),
        widget=UnfoldAdminSelectWidget,
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)

        qs = ProductionResource.objects.filter(is_active=True).order_by("id")
        if company is not None:
            qs = qs.filter(company=company)
        self.fields["resource_id"].queryset = qs
