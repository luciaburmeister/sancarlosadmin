from django.shortcuts import render, redirect, get_object_or_404
from django.views.generic import TemplateView, FormView
from configurator.models import ProductTemplate
from unfold.views import UnfoldModelAdminViewMixin
from configurator.forms import ConfigurationForm
from django.urls import reverse
class ProductSelectionView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Select Product to Configure" 
    permission_required = ('configurator.view_producttemplate') 
    template_name = "configurator/product_selection.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["products"] = ProductTemplate.objects.all()
        return context


class ConfigurationWizardView(UnfoldModelAdminViewMixin, FormView):
    template_name = "configurator/configuration_wizard.html"
    form_class = ConfigurationForm
    permission_required = ('configurator.view_producttemplate',)

    title = "Configure Product"

    def dispatch(self, request, product_id, *args, **kwargs):
        """
        Load product once for the whole request.
        """
        self.product = get_object_or_404(ProductTemplate, pk=product_id)
        return super().dispatch(request, product_id, *args, **kwargs)

    def get_form_kwargs(self):
        """
        Inject the product into the form.
        """
        kwargs = super().get_form_kwargs()
        kwargs["product"] = self.product
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context.update({
            "product": self.product,
        })

        return context

    def form_valid(self, form):
        """
        Save configuration and redirect to admin change page using reverse().
        """
        config = form.save()
        url = reverse(
            "admin:configurator_configuration_change",
            args=[config.id]
        )
        return redirect(url)