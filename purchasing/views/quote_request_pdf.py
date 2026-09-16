from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from core.services.pdf_service import PDFService
from ..models import QuoteRequest


class QuoteRequestPdfView(View):
    """Browser preview: rendered HTML of the quote request export template."""

    model_admin = None

    def get(self, request, object_id):
        quote_request = get_object_or_404(QuoteRequest, pk=object_id)
        template = ConfigurationExportTemplate.get_default_for_product(
            product=None,
            kind=ExportTemplateKind.QUOTE_REQUEST,
            company=quote_request.company,
        )
        if not template:
            return HttpResponse("No quote request template found.", status=404)

        html = ConfigurationExportService.render_html(quote_request, template)
        return HttpResponse(html)


class QuoteRequestPdfDownloadView(View):
    """Force-download a WeasyPrint PDF of a quote request."""

    model_admin = None

    def get(self, request, object_id):
        quote_request = get_object_or_404(
            QuoteRequest.objects.select_related("company"), pk=object_id
        )
        pdf_bytes = PDFService.render_object_pdf(
            quote_request,
            ExportTemplateKind.QUOTE_REQUEST,
            company=quote_request.company,
        )
        if not pdf_bytes:
            return HttpResponse("No quote request template found.", status=404)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="Solicitud_Cotizacion_{quote_request.number}.pdf"'
        return response
