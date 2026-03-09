from __future__ import annotations
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminSelectWidget
from production.models import ProductionResource
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Div, Field

class ScheduleFilterForm(forms.Form):
    day = forms.DateField(
        label=_("Day"),
        required=True,
        initial=timezone.localdate,
        widget=UnfoldAdminDateWidget(),
    )

    resource_id = forms.ModelChoiceField(
        label=_("Production resource"),
        required=False,
        queryset=ProductionResource.objects.none(),
        empty_label=_("All resources"),
        widget=UnfoldAdminSelectWidget(),
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)

        qs = ProductionResource.objects.filter(is_active=True).order_by("id")
        if company is not None:
            qs = qs.filter(company=company)
        self.fields["resource_id"].queryset = qs

        self.helper = FormHelper()
        self.helper.form_tag = False 
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            Div(
                Div(Field("day"), css_class="min-w-[200px]"),
                Div(Field("resource_id"), css_class="min-w-[320px] max-w-[560px] flex-1"),
                css_class="flex flex-wrap items-end gap-4",
            )
        )
