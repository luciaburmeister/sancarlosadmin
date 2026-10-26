from datetime import timedelta

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminSelectWidget

from company.models import BankAccount

# Days the bank needs between receiving the file and charging the client
MIN_BUSINESS_DAYS_AHEAD = 2


def add_business_days(day, count):
    while count:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count -= 1
    return day


class SepaDirectDebitForm(forms.Form):
    creditor_account = forms.ModelChoiceField(
        queryset=BankAccount.objects.none(),
        label=_("Collect into"),
        widget=UnfoldAdminSelectWidget(),
    )
    collection_date = forms.DateField(
        label=_("Collection date"),
        widget=UnfoldAdminDateWidget(),
        help_text=_("Day the bank charges the clients."),
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = BankAccount.objects.filter(company=company).order_by("-is_default", "bank_name")
        self.fields["creditor_account"].queryset = accounts
        if not self.is_bound:
            self.initial.setdefault("creditor_account", accounts.first())
            self.initial.setdefault(
                "collection_date",
                add_business_days(timezone.localdate(), MIN_BUSINESS_DAYS_AHEAD + 1),
            )

    def clean_collection_date(self):
        value = self.cleaned_data["collection_date"]
        earliest = add_business_days(timezone.localdate(), MIN_BUSINESS_DAYS_AHEAD)
        if value < earliest:
            raise forms.ValidationError(
                _("The bank needs the file at least %(days)d business days before: pick %(date)s or later.")
                % {"days": MIN_BUSINESS_DAYS_AHEAD, "date": earliest.strftime("%d/%m/%Y")}
            )
        return value
