from django.views.generic import TemplateView
from configurator.models import ProductTemplate
from unfold.views import UnfoldModelAdminViewMixin


class ProductSelectionView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Select Product to Configure" 
    permission_required = ('configurator.view_producttemplate') 
    template_name = "configurator/product_selection.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["products"] = ProductTemplate.objects.all()
        return context
