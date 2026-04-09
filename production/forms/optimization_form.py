from django import forms
from configurator.models import Configuration
from django.utils.translation import gettext_lazy as _

from unfold.widgets import (
    UnfoldAdminSelect2Widget,
    UnfoldAdminDateWidget,
    UnfoldAdminIntegerFieldWidget,
)

from unfold.fields import UnfoldAdminMultipleAutocompleteModelChoiceField


STRATEGY_CHOICES = [
    ("margin", "Margin"),
]


class OptimizeScheduleForm(forms.Form):
    strategy = forms.ChoiceField(
        choices=STRATEGY_CHOICES,
        widget=UnfoldAdminSelect2Widget,
        initial="margin",
        help_text="Optimization strategy to use.",
    )
    start_date = forms.DateField(
        widget=UnfoldAdminDateWidget,
        help_text="First day that can be scheduled.",
    )
    end_date = forms.DateField(
        widget=UnfoldAdminDateWidget,
        help_text="Last day that can be scheduled.",
    )
    ts = forms.IntegerField(
        min_value=1,
        initial=10,
        widget=UnfoldAdminIntegerFieldWidget,
        label="Time step (minutes)",
        help_text="Slot size used by the optimizer.",
    )
    configurations = UnfoldAdminMultipleAutocompleteModelChoiceField(
        label=_("Configurations"),
        queryset=Configuration.objects.all(),
        url_path="admin:production_configuration_autocomplete",
        required=True,
        help_text="Configurations to include in the optimization run.",
    )

    def __init__(self, *args, **kwargs):
        request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        if request is None:
            self.fields["configurations"].queryset = Configuration.objects.none()
            return

        if request.method == "POST":
            self.fields["configurations"].queryset = Configuration.objects.filter(
                pk__in=request.POST.getlist("configurations")
            )
        else:
            self.fields["configurations"].queryset = Configuration.objects.none()

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        ts = cleaned.get("ts")

        if start_date and end_date and end_date < start_date:
            raise forms.ValidationError("End date must be on or after start date.")

        if ts and 60 % ts != 0:
            raise forms.ValidationError("Time step should divide 60 cleanly.")

        return cleaned
    
    class Media:
        js = [
            f"admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]
