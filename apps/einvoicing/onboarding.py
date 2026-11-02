from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.einvoicing.models import EInvoicingAccount
from company.models import CompanyModule
from general.onboarding import MISSING, OK, Check, OnboardingStep, Section, register


@register
class EInvoicingStep(OnboardingStep):
    key = "einvoicing"
    title = gettext_lazy("E-invoicing")
    description = gettext_lazy("The account your invoices are sent to the tax agency through.")
    order = 55
    module = CompanyModule.EINVOICING

    def sections(self):
        account = EInvoicingAccount.objects.filter(company=self.company).first()
        return [Section(_("E-invoicing"), [
            Check(_("Account"), OK, str(account),
                  reverse("admin:einvoicing_einvoicingaccount_change", args=[account.pk]))
            if account else
            Check(_("Account"), MISSING, _("Invoices aren't sent to the tax agency."),
                  reverse("admin:einvoicing_einvoicingaccount_add")),
        ])]
