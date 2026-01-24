from configurator.models import ProductTemplate
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView
 

class ProductSelectionView(UnfoldModelAdminViewMixin, ListView):
    title = _("Select Product to Configure")
    permission_required = ('configurator.view_producttemplate') 
    template_name = "configurator/product_selection.html"

    model = ProductTemplate
    paginate_by = 12
    context_object_name = "products"

