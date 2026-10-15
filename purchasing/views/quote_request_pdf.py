from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from core.documents import enqueue_document
from purchasing.documents import render_quote_request_pdf
from django.urls import reverse
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
    """Download a WeasyPrint PDF of a quote request, rendered on the pdf queue."""

    model_admin = None

    def get(self, request, object_id):
        quote_request = get_object_or_404(QuoteRequest, pk=object_id)
        return enqueue_document(
            request,
            render_quote_request_pdf,
            quote_request_id=quote_request.pk,
            filename=f"Solicitud_Cotizacion_{quote_request.number}.pdf",
            back_url=reverse("admin:purchasing_quoterequest_change", args=[quote_request.pk]),
        )
