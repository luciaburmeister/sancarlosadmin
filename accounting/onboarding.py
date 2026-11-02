from django import forms
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from accounting.models import CompanyChartOfAccounts
from general.onboarding import OnboardingStep, register
from general.onboarding.forms import UnfoldFormMixin


class GoLiveForm(UnfoldFormMixin, forms.Form):
    ledger_start_date = forms.DateField(
        label=gettext_lazy("Go-live date"),
        help_text=gettext_lazy(
            "The first day the ERP keeps your books. Invoices and payments dated earlier, "
            "like the open invoices you import, stay out of the ledger: their balances come "
            "in with the opening entry, dated this day."
        ),
    )

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.chart = CompanyChartOfAccounts.objects.filter(company=company).first()
        if self.chart:
            self.fields["ledger_start_date"].initial = self.chart.ledger_start_date

    def clean(self):
        if self.chart is None:
            raise forms.ValidationError(_("The company has no chart of accounts yet."))
        return super().clean()


@register
class AccountingStep(OnboardingStep):
    key = "accounting"
    title = gettext_lazy("Accounting")
    description = gettext_lazy(
        "The chart of accounts, fiscal year and accounts your invoices and payments are booked to."
    )
    order = 30
    form_class = GoLiveForm
    submit_label = gettext_lazy("Save")

    def get_form_kwargs(self):
        return {"company": self.company}

    def sections(self):
        from accounting.services.setup_check import AccountingSetupCheck

        return AccountingSetupCheck(self.company).configuration()

    def save(self, form, request):
        from company.services.onboarding import ensure_fiscal_year

        go_live = form.cleaned_data["ledger_start_date"]
        form.chart.ledger_start_date = go_live
        form.chart.save(update_fields=["ledger_start_date"])
        for year in sorted({go_live.year, timezone.localdate().year}):
            ensure_fiscal_year(self.company, year)
        return _("Go-live date set to %(date)s.") % {"date": go_live}
