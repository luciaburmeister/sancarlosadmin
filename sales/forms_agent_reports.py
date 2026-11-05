from datetime import timedelta

from django import forms
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminSelectWidget

from configurator.forms import ConfigurationExportForm
from configurator.services.export.commissionist_agent import SettlementScope
from core.forms.unfold_date_filters import DateRangeFilterForm


class AgentReportFormat(models.TextChoices):
    PDF = "pdf", _("PDF")
    XLSX = "xlsx", _("Excel")


class AgentReportExportForm(DateRangeFilterForm, ConfigurationExportForm):
    file_format = forms.ChoiceField(
        label=_("Format"),
        choices=AgentReportFormat.choices,
        initial=AgentReportFormat.PDF,
        widget=UnfoldAdminSelectWidget,
    )
    settlement_scope = forms.ChoiceField(
        label=_("Commissions"),
        choices=SettlementScope.choices,
        initial=SettlementScope.PERIOD,
        widget=UnfoldAdminSelectWidget,
        help_text=_(
            "What the settlement lists: the commission released in the period, "
            "what no settlement has paid up to the end date (whenever it was "
            "released), or the lines of a recorded settlement."
        ),
    )
    settlement = forms.ModelChoiceField(
        label=_("Settlement"),
        queryset=None,
        required=False,
        widget=UnfoldAdminSelectWidget,
        help_text=_("The recorded settlement to print."),
    )

    def __init__(self, *args, agent, **kwargs):
        super().__init__(*args, **kwargs)
        # Reports are run at month end close, so default to the last closed month.
        last_day = timezone.localdate().replace(day=1) - timedelta(days=1)
        self.fields["date_from"].initial = last_day.replace(day=1)
        self.fields["date_to"].initial = last_day
        self.fields["date_from"].required = True
        self.fields["date_to"].required = True
        self.fields["settlement"].queryset = agent.commission_settlements.confirmed()

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("settlement_scope") != SettlementScope.RECORDED:
            cleaned_data["settlement"] = None
        elif not cleaned_data.get("settlement") and "settlement" not in self.errors:
            self.add_error("settlement", _("Choose the settlement to print."))
        return cleaned_data

    def get_period(self):
        return self.cleaned_data["date_from"], self.cleaned_data["date_to"]
