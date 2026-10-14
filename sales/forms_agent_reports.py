from datetime import timedelta

from django.utils import timezone

from configurator.forms import ConfigurationExportForm
from core.forms.unfold_date_filters import DateRangeFilterForm


class AgentReportExportForm(DateRangeFilterForm, ConfigurationExportForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Reports are run at month end close, so default to the last closed month.
        last_day = timezone.localdate().replace(day=1) - timedelta(days=1)
        self.fields["date_from"].initial = last_day.replace(day=1)
        self.fields["date_to"].initial = last_day
        self.fields["date_from"].required = True
        self.fields["date_to"].required = True

    def get_period(self):
        return self.cleaned_data["date_from"], self.cleaned_data["date_to"]
