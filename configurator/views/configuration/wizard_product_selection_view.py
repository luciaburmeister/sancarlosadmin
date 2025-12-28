from django.views.generic import TemplateView
from configurator.models import ProductTemplate
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _
from django.core.paginator import Paginator


class ProductSelectionView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Select Product to Configure")
    permission_required = ('configurator.view_producttemplate') 
    template_name = "configurator/product_selection.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        products_qs = ProductTemplate.objects.all().order_by("name")

        paginator = Paginator(products_qs, 12)
        page_number = self.request.GET.get("page")
        page_obj = paginator.get_page(page_number)

        context["page_obj"] = page_obj
        context["products"] = page_obj.object_list

        return context
