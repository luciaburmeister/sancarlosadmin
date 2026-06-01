from django.db.models import Q
from configurator.models import ProductTemplate
from unfold.views import UnfoldModelAdminViewMixin
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView
from core.views.company_queryset import CompanyRestrictedQuerysetMixin


class ProductSelectionView(UnfoldModelAdminViewMixin,CompanyRestrictedQuerysetMixin, ListView):
    title = _("Select Product to Configure")
    permission_required = ('configurator.view_producttemplate') 
    template_name = "configurator/product_selection.html"

    model = ProductTemplate
    paginate_by = 20
    context_object_name = "products"
    
    def get_queryset(self):
        qs = super().get_queryset().filter(show_in_configurator=True)
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
        return qs.order_by("name")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['search_query'] = self.request.GET.get('q', '')
        if self.request.GET.get('quote_id'):
            context['quote_id'] = self.request.GET.get('quote_id')
        return context
