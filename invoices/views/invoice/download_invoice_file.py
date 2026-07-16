from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from invoices.models import Invoice
from invoices.services.invoice_pdf_download import InvoicePdfDownloadService


class PurchaseInvoiceFileDownloadView(View):
    """Force-download the uploaded purchase invoice file."""

    model_admin = None

    def get(self, request, object_id):
        invoice = get_object_or_404(Invoice, pk=object_id)
        content = InvoicePdfDownloadService.read_purchase_invoice_file(invoice)
        if content is None:
            return HttpResponse("No file attached to this invoice.", status=404)

        filename = InvoicePdfDownloadService.purchase_invoice_filename(invoice)
        content_type = "application/pdf"
        lower = filename.lower()
        if lower.endswith(".png"):
            content_type = "image/png"
        elif lower.endswith(".jpg") or lower.endswith(".jpeg"):
            content_type = "image/jpeg"

        return InvoicePdfDownloadService.pdf_attachment_response(
            content, filename, content_type=content_type
        )
