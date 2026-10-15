from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from core.documents import enqueue_document
from invoices.documents import render_sales_invoice_pdf
from invoices.models import SalesInvoice
from invoices.services.invoice_pdf_download import InvoicePdfDownloadService
from invoices.services.sales_invoice_service import SalesInvoiceService


class SalesInvoicePdfView(View):
    """Browser preview: rendered HTML of the sales invoice export template."""

    model_admin = None

    def get(self, request, object_id):
        invoice = get_object_or_404(SalesInvoice, pk=object_id)
        html = SalesInvoiceService.render_pdf(invoice)

        if not html:
            return HttpResponse("No invoice template found.", status=404)

        return HttpResponse(html)


class SalesInvoicePdfDownloadView(View):
    """Download a WeasyPrint PDF of a sales invoice, rendered on the pdf queue."""

    model_admin = None

    def get(self, request, object_id):
        invoice = get_object_or_404(SalesInvoice, pk=object_id)
        return enqueue_document(
            request,
            render_sales_invoice_pdf,
            invoice_id=invoice.pk,
            filename=InvoicePdfDownloadService.sales_invoice_pdf_filename(invoice),
            back_url=reverse("admin:invoices_salesinvoice_change", args=[invoice.pk]),
        )
