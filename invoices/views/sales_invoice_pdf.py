from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

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
    """Force-download a WeasyPrint PDF of a sales invoice."""

    model_admin = None

    def get(self, request, object_id):
        invoice = get_object_or_404(
            SalesInvoice.objects.select_related("company", "client", "configuration"),
            pk=object_id,
        )
        pdf_bytes = InvoicePdfDownloadService.render_sales_invoice_pdf(invoice)
        if not pdf_bytes:
            return HttpResponse("No invoice template found.", status=404)
        return InvoicePdfDownloadService.pdf_attachment_response(
            pdf_bytes,
            InvoicePdfDownloadService.sales_invoice_pdf_filename(invoice),
        )
