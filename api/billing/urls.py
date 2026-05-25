from django.urls import path

from .views.invoices import QuoteInvoiceView
from .views.expediente_invoices import (
    ProjectFileInvoiceCreateView,
    SalesInvoiceDetailView,
    SalesInvoiceFinalizeView,
    SalesInvoiceListView,
    SalesInvoicePaymentView,
    SalesInvoicePdfView,
    SalesInvoiceSendEmailView,
)

urlpatterns = [
    path("quotes/<int:quote_id>/invoice/", QuoteInvoiceView.as_view(), name="quote_invoice"),
    path("sales-invoices/", SalesInvoiceListView.as_view(), name="sales_invoice_list"),
    path(
        "project-files/<int:project_file_id>/invoices/",
        ProjectFileInvoiceCreateView.as_view(),
        name="project_file_invoice_create",
    ),
    path(
        "sales-invoices/<int:invoice_id>/",
        SalesInvoiceDetailView.as_view(),
        name="sales_invoice_detail",
    ),
    path(
        "sales-invoices/<int:invoice_id>/pdf/",
        SalesInvoicePdfView.as_view(),
        name="sales_invoice_pdf",
    ),
    path(
        "sales-invoices/<int:invoice_id>/finalize/",
        SalesInvoiceFinalizeView.as_view(),
        name="sales_invoice_finalize",
    ),
    path(
        "sales-invoices/<int:invoice_id>/send-email/",
        SalesInvoiceSendEmailView.as_view(),
        name="sales_invoice_send_email",
    ),
    path(
        "sales-invoices/<int:invoice_id>/payments/",
        SalesInvoicePaymentView.as_view(),
        name="sales_invoice_payments",
    ),
]
