from django.views import View
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from invoices.models import SalesInvoice
from invoices.services.sales_invoice_service import SalesInvoiceService

class SalesInvoicePdfView(View):
    model_admin = None

    def get(self, request, object_id):
        invoice = get_object_or_404(SalesInvoice, pk=object_id)
        html = SalesInvoiceService.render_pdf(invoice)
        
        if not html:
            return HttpResponse("No invoice template found.", status=404)

        return HttpResponse(html)
