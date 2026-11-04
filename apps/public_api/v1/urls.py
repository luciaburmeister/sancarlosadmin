from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

from .clients.views import ClientDetailView, ClientListView
from .sales_invoices.views import SalesInvoiceDetailView, SalesInvoiceFinalizeView, SalesInvoiceListCreateView
from .tax_types.views import TaxTypeListView

app_name = "public_api_v1"

urlpatterns = [
    path("clients/", ClientListView.as_view(), name="client_list"),
    path("clients/<int:client_id>/", ClientDetailView.as_view(), name="client_detail"),
    path("sales-invoices/", SalesInvoiceListCreateView.as_view(), name="sales_invoice_list"),
    path("sales-invoices/<int:invoice_id>/", SalesInvoiceDetailView.as_view(), name="sales_invoice_detail"),
    path(
        "sales-invoices/<int:invoice_id>/finalize/", SalesInvoiceFinalizeView.as_view(), name="sales_invoice_finalize",
    ),
    path("tax-types/", TaxTypeListView.as_view(), name="tax_type_list"),
    path("schema/", SpectacularAPIView.as_view(api_version="1.0.0"), name="schema"),
    path("docs/", SpectacularRedocView.as_view(url_name="public_api_v1:schema"), name="docs"),
]
