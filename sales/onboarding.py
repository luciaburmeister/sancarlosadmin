from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from general.onboarding import OK, WARNING, Check, Column, ImportPrompt, OnboardingStep, Section, register


@register
class ClientsAndProvidersStep(OnboardingStep):
    key = "partners"
    title = gettext_lazy("Clients and providers")
    description = gettext_lazy(
        "Bring your clients over from your previous system with a spreadsheet. Providers come in with "
        "the open purchase invoices you import; add any others by hand. Invoices find their client or "
        "provider by tax identifier."
    )
    order = 80
    required = False

    def sections(self):
        from materials.models import Provider
        from sales.models import Client

        clients = Client.objects.filter(company=self.company).count()
        providers = Provider.objects.filter(company=self.company).count()
        clients_url = reverse("admin:sales_client_import")
        # The provider import only updates payment methods of existing providers
        providers_url = reverse("admin:materials_provider_add")
        return [Section(_("Clients and providers"), [
            Check(_("Clients"), OK, _("%(count)s so far.") % {"count": clients}, clients_url)
            if clients else
            Check(_("Clients"), WARNING, _("None yet: import them from a spreadsheet."), clients_url),
            Check(_("Providers"), OK, _("%(count)s so far.") % {"count": providers}, providers_url)
            if providers else
            Check(_("Providers"), WARNING,
                  _("None yet: they're created when you import open purchase invoices, or add them by hand."),
                  providers_url),
        ])]

    def import_prompts(self):
        from sales.resources import COL_ADDRESS, COL_CITY, COL_EMAIL, COL_NAME, COL_PHONE, COL_POSTAL, COL_PROVINCE, COL_TAX

        return [ImportPrompt(
            title=_("Clients"),
            rows=_("one row per client"),
            columns=[
                Column(COL_NAME, _("the client's name."), required=True),
                Column(COL_TAX, _("their tax identifier (NIF/CIF/VAT number), without spaces or dashes.")),
                Column(COL_ADDRESS, _("street and number.")),
                Column(COL_POSTAL, _("postal code.")),
                Column(COL_CITY, _("town or city.")),
                Column(COL_PROVINCE, _("province.")),
                Column(COL_PHONE, _("phone number.")),
                Column(COL_EMAIL, _("email address; just one.")),
            ],
            rules=[_("One row per client: merge duplicates of the same tax identifier into one row.")],
        )]
