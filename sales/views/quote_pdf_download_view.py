from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from configurator.models import ExportTemplateKind
from core.services.pdf_service import PDFService
from sales.models import Quote


class QuotePdfDownloadView(View):
    """Force-download a WeasyPrint PDF of a quote using its default quotation template."""

    model_admin = None

    def get(self, request, quote_id):
        quote = get_object_or_404(
            Quote.objects.select_related("company", "client"), pk=quote_id
        )
        pdf_bytes = PDFService.render_object_pdf(
            quote,
            ExportTemplateKind.CONSOLIDATED_QUOTATION,
            company=quote.company,
        )
        if not pdf_bytes:
            return HttpResponse("No quote template found.", status=404)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="Presupuesto_{quote.pk}.pdf"'
        return response
