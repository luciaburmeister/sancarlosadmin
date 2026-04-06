"""
Inventory autocomplete views — used by the movement form widgets.
Registered in InventoryItemAdmin.get_urls().
"""

from django.db.models import Q

from unfold.views import BaseAutocompleteView

from inventory.models import Warehouse
from materials.models import Provider
from invoices.models import Invoice


class WarehouseAutocompleteView(BaseAutocompleteView):
    model = Warehouse

    def get_queryset(self):
        qs = Warehouse.objects.filter(is_active=True)
        term = self.request.GET.get("term", "")
        if term:
            qs = qs.filter(
                Q(name__icontains=term) | Q(code__icontains=term)
            )
        return qs


class ProviderAutocompleteView(BaseAutocompleteView):
    model = Provider

    def get_queryset(self):
        qs = Provider.objects.all()
        term = self.request.GET.get("term", "")
        if term:
            qs = qs.filter(name__icontains=term)
        return qs


class InvoiceAutocompleteView(BaseAutocompleteView):
    model = Invoice

    def get_queryset(self):
        qs = Invoice.objects.select_related("provider").order_by("-date_issued")
        term = self.request.GET.get("term", "")
        if term:
            qs = qs.filter(
                Q(invoice_number__icontains=term) |
                Q(provider__name__icontains=term)
            )
        return qs