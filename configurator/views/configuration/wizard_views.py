from django.shortcuts import redirect, get_object_or_404
from configurator.models import ProductTemplate,ConfigurationItem
from unfold.views import UnfoldModelAdminViewMixin
from configurator.forms import ConfigurationForm,AddonForm,BaseAddonFormSet,ConfigurationFormHelper,AddonFormsetHelper,ClientSelectionForm,ClientSelectionFormHelper
from django.urls import reverse
from formtools.wizard.views import SessionWizardView
from django.forms import formset_factory
from django.utils.translation import gettext_lazy as _
from sales.models import Quote

class ConfigurationWizardView(UnfoldModelAdminViewMixin,SessionWizardView):
    form_list = [
        ("client_selection", ClientSelectionForm),
        ("measures", ConfigurationForm),
        ("addons", formset_factory(AddonForm, extra=1, can_delete=True,formset=BaseAddonFormSet))
    ]
    template_name = "configurator/wizard_form.html"
    permission_required = ('configurator.view_producttemplate') 
    title = _("Configure your product")

    
    def dispatch(self, request, product_id, *args, **kwargs):
        self.product = get_object_or_404(ProductTemplate, pk=product_id)
        return super().dispatch(request, product_id, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["product"] = self.product
        kwargs["request"] = self.request
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
        elif self.steps.current == "addons":
            helper = AddonFormsetHelper()
            context["helper"] = helper
            context["formset_id"] = helper.formset_id
            context["form_id"] = helper.form_id
            context["form_add"] = helper.form_add

        return context


    def get(self, request, *args, **kwargs):
        if 'quote_id' in request.GET:
            request.session['wizard_quote_id'] = request.GET['quote_id']
        return super().get(request, *args, **kwargs)

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)
        if step == 'client_selection':
            quote_id = self.request.GET.get('quote_id') or self.request.session.get('wizard_quote_id')
            if quote_id:
                quote = Quote.objects.filter(id=quote_id).first()
                if quote:
                    initial['quote'] = quote
        return initial

    def done(self, form_list, **kwargs):
        client_selection_form = form_list[0]
        config_form = form_list[1]
        addon_formset = form_list[2] if len(form_list) > 2 else None
        
        quote = client_selection_form.cleaned_data.get("quote")
        client = quote.client if quote else None
        asset = client_selection_form.cleaned_data.get("asset")
        
        config = config_form.save(client=client, asset=asset)
        
        if addon_formset:
            for form in addon_formset:
                if not form.cleaned_data:
                    continue
                if form.cleaned_data.get("DELETE"):
                    continue
                component = form.cleaned_data.get("addons")
                quantity = form.cleaned_data.get("quantity", 1)

                if component:
                    ConfigurationItem.objects.create(
                        configuration=config,
                        component=component,
                        quantity=quantity,
                        unit_price=component.cost_per_unit,
                        is_template_item=False,
                    )

        self.request.session.pop('wizard_quote_id', None)
        if quote:
            config.quote = quote
            config.save(update_fields=['quote'])

        config.copy_default_pricing_rules_from_product()
        config.copy_default_taxes_from_product()
        config.recalculate_price()
        
        
        url = reverse(
                "admin:configurator_configuration_change",
                args=[config.id]
            )
        return redirect(url)