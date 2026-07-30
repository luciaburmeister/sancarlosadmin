from django import forms
from django.utils.translation import gettext_lazy as _

from unfold.widgets import UnfoldAdminDateWidget, UnfoldAdminSelect2Widget, UnfoldAdminSelect2MultipleWidget


class PaymentCalendarFilterForm(forms.Form):

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]

    company = forms.ChoiceField(
        required=False,
        label=_("Empresa"),
        choices=[],
        widget=UnfoldAdminSelect2Widget(),
    )

    bank_accounts = forms.MultipleChoiceField(
        required=False,
        label=_("Cuentas bancarias"),
        choices=[],
        widget=UnfoldAdminSelect2MultipleWidget(),
    )

    date_from = forms.DateField(
        required=False,
        label=_("Desde"),
        widget=UnfoldAdminDateWidget(attrs={"class": "vDateField"}),
    )

    date_to = forms.DateField(
        required=False,
        label=_("Hasta"),
        widget=UnfoldAdminDateWidget(attrs={"class": "vDateField"}),
    )

    def __init__(self, *args, companies=None, bank_accounts=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].choices = [
            (str(company.pk), str(company)) for company in (companies or [])
        ]
        self.fields["bank_accounts"].choices = [
            (str(account.pk), str(account)) for account in (bank_accounts or [])
        ]