from django.shortcuts import redirect, get_object_or_404
from configurator.models import ProductTemplate
from unfold.views import UnfoldModelAdminViewMixin
from configurator.forms import ConfigurationForm, ConfigurationFormHelper, ClientSelectionForm, ClientSelectionFormHelper
from django.urls import reverse
from formtools.wizard.views import SessionWizardView
from django.utils.translation import gettext_lazy as _
from sales.models import Quote

class ConfigurationWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    form_list = [
        ("client_selection", ClientSelectionForm),
        ("measures", ConfigurationForm),
    ]
    template_name = "configurator/wizard_form.html"
    permission_required = ('configurator.view_producttemplate')
    title = _("Configure your product")

    def dispatch(self, request, product_id=None, config_id=None, *args, **kwargs):
        self.instance = None
        if config_id:
            from configurator.models import Configuration
            self.instance = get_object_or_404(Configuration, pk=config_id)
            self.product = self.instance.product
        else:
            self.product = get_object_or_404(ProductTemplate, pk=product_id)

        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["product"] = self.product
        kwargs["request"] = self.request
        if step == "measures":
            kwargs["instance"] = self.instance
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product"] = self.product
        kwargs['prefix'] = self.steps.current

        if self.steps.current == "client_selection":
            helper = ClientSelectionFormHelper()
            context["helper"] = helper
        elif self.steps.current == "measures":
            helper = ConfigurationFormHelper(self.get_form(step=self.steps.current))
            context["helper"] = helper

        return context

    def get(self, request, *args, **kwargs):
        if 'quote_id' in request.GET:
            request.session['wizard_quote_id'] = request.GET['quote_id']
        return super().get(request, *args, **kwargs)

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)
        if step == 'client_selection':
            if self.instance:
                initial['quote'] = self.instance.quote
                initial['client'] = self.instance.client
                initial['asset'] = self.instance.asset
            else:
                quote_id = self.request.GET.get('quote_id') or self.request.session.get('wizard_quote_id')
                if quote_id:
                    quote = Quote.objects.filter(id=quote_id).first()
                    if quote:
                        initial['quote'] = quote
        return initial

    def done(self, form_list, **kwargs):
        form_list = list(form_list)
        client_selection_form = form_list[0]
        config_form = form_list[1]

        quote = client_selection_form.cleaned_data.get("quote")
        client = quote.client if quote else client_selection_form.cleaned_data.get("client")
        asset = client_selection_form.cleaned_data.get("asset")

        config = config_form.save(client=client, asset=asset)

        self.request.session.pop('wizard_quote_id', None)
        if quote:
            config.quote = quote
            config.save(update_fields=['quote'])

        if not self.instance:
            config.copy_default_pricing_rules_from_product()
            config.copy_default_taxes_from_product()
            config.copy_taxes_from_client()

        config.recalculate_price()

        # If editing and production tickets exist, regenerate their HTML
        if self.instance:
            from django.contrib.contenttypes.models import ContentType
            from production.models import ProductionTicket
            from production.services.configuration_to_production import ConfigurationToProductionService

            config_ct = ContentType.objects.get_for_model(config)
            for ticket in ProductionTicket.objects.filter(
                content_type=config_ct, object_id=config.id
            ):
                ConfigurationToProductionService._assign_default_templates(ticket, config)

        url = reverse(
                "admin:configurator_configuration_change",
                args=[config.id]
            )
        return redirect(url)