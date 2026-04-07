from unfold.views import BaseAutocompleteView
from invoices.models import TaxType
from django.db.models import Q

class TaxTypeAutocompleteView(BaseAutocompleteView):
    model = TaxType

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.filter(is_active=True)
            
        term = self.request.GET.get("term")
        if term:
            qs = qs.filter(
                Q(tax_name__icontains=term) | 
                Q(country__icontains=term)
            ).distinct()
            
        return qs
