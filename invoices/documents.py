"""Renderers for the invoice documents downloaded from the admin (see core.documents)."""
from core.documents import document_renderer
from invoices.models import SalesInvoice
from invoices.services.invoice_pdf_download import InvoicePdfDownloadService


@document_renderer
def render_sales_invoice_pdf(invoice_id: int) -> bytes | None:
    invoice = SalesInvoice.objects.select_related("company", "client", "configuration").get(pk=invoice_id)
    return InvoicePdfDownloadService.render_sales_invoice_pdf(invoice)
