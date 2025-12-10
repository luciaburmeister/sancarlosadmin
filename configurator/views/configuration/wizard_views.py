from django.shortcuts import redirect, get_object_or_404
from configurator.models import ProductTemplate,ConfigurationItem
from unfold.views import UnfoldModelAdminViewMixin
from configurator.forms import ConfigurationForm,AddonForm,BaseAddonFormSet
from django.urls import reverse
from django.shortcuts import get_object_or_404
from formtools.wizard.views import SessionWizardView
from django.forms import formset_factory


class ConfigurationWizardView(UnfoldModelAdminViewMixin,SessionWizardView):
    form_list = [
        ("measures", ConfigurationForm),
        ("addons", formset_factory(AddonForm, extra=1, can_delete=True,formset=BaseAddonFormSet))
    ]
    template_name = "configurator/wizard_form.html"
    permission_required = ('configurator.view_producttemplate') 
    title = "Configure your product" 

    
    def dispatch(self, request, product_id, *args, **kwargs):
        self.product = get_object_or_404(ProductTemplate, pk=product_id)
        return super().dispatch(request, product_id, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        kwargs["product"] = self.product
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["product"] = self.product
        return context

    def done(self, form_list, **kwargs):
        config_form = form_list[0]
        addon_formset = form_list[1] if len(form_list) > 1 else None
        config = config_form.save()
        
        for form in addon_formset:
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            material = form.cleaned_data.get("addons")
            quantity = form.cleaned_data.get("quantity", 1)

            if material:
                ConfigurationItem.objects.create(
                    configuration=config,
                    material=material,
                    quantity=quantity,
                    unit_price=material.cost_per_unit,
                    is_template_item=False,
                )
        config.recalculate_price()
        
        url = reverse(
            "admin:configurator_configuration_change",
            args=[config.id]
        )
        return redirect(url)