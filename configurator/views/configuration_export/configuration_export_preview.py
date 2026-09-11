import base64
import json
import logging

from django.views import View
from django.http import HttpResponse, Http404, JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_exempt
from configurator.models import ConfigurationExportTemplate
from configurator.services.export import ConfigurationExportService
from core.services.pdf_service import PDFService
from unfold.views import UnfoldModelAdminViewMixin

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(xframe_options_exempt, name='dispatch')
class ExportTemplatePreviewView(UnfoldModelAdminViewMixin, View):
    permission_required = "configurator.view_configurationexporttemplate"
    """
    Renders a preview of an export template.

    The preview is the real WeasyPrint PDF, not the intermediate HTML, so what
    the editor shows is exactly what clients receive by email or download.
    Pass format=html to inspect the intermediate HTML instead.

    GET  preview/<template_id>/            -> inline PDF (or HTML)
    POST preview/live/  (html, css, ...)   -> JSON {"pdf": <base64>, "context": {...}}
                                              or, with format=html, the HTML page
    """

    def _get_preview_object(self, kind, product_id, company_id, object_id=None):
        from configurator.services.export.kind_registry import get_preview_object
        return get_preview_object(
            kind,
            product_id=product_id,
            company_id=company_id,
            object_id=object_id,
        )

    @staticmethod
    def _pdf_response(pdf_bytes: bytes) -> HttpResponse:
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="preview.pdf"'
        return response

    def post(self, request, *args, **kwargs):
        html_content = request.POST.get("html", "")
        css_content = request.POST.get("css", "")
        output_format = request.POST.get("format", "json")

        object_id = request.POST.get("object_id") or request.POST.get("config_id")
        template_kind = request.POST.get("kind", "quotation")
        product_id = request.POST.get("product")
        company_id = request.POST.get("company")

        target_obj = self._get_preview_object(template_kind, product_id, company_id, object_id)

        if not target_obj:
            return self._error("No suitable object available for preview.", 400, output_format)

        template = ConfigurationExportTemplate(
            html=html_content,
            css=css_content,
            kind=template_kind
        )

        try:
            rendered_body, clean_css, serialized_context = ConfigurationExportService.render_parts(
                target_obj, template
            )
        except Exception as exc:
            # Template syntax errors are the common case while someone is typing;
            # surface them in the editor instead of a blank frame.
            logger.info("Export template preview failed to render: %s", exc)
            return self._error(f"Template error: {exc}", 400, output_format)

        rendered_html = ConfigurationExportService.wrap_parts(rendered_body, clean_css, serialized_context)

        if output_format == "html":
            return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")

        try:
            pdf_bytes = PDFService.html_to_pdf(rendered_html)
        except RuntimeError as exc:
            return self._error(str(exc), 503, output_format)
        except Exception as exc:
            logger.exception("Export template preview PDF rendering failed")
            return self._error(f"PDF rendering error: {exc}", 500, output_format)

        return JsonResponse({
            "pdf": base64.b64encode(pdf_bytes).decode("ascii"),
            "context": json.loads(serialized_context),
        })

    def get(self, request, *args, **kwargs):
        template_id = kwargs.get("template_id")
        try:
            template = ConfigurationExportTemplate.objects.get(id=template_id)
        except ConfigurationExportTemplate.DoesNotExist:
            raise Http404("Template not found")

        output_format = request.GET.get("format", "pdf")
        object_id = request.GET.get("object_id")
        target_obj = self._get_preview_object(template.kind, template.product_id, template.company_id, object_id)

        if not target_obj:
            return HttpResponse("No suitable object available for preview.", status=400)

        rendered_html = ConfigurationExportService.render_html(
            target_obj, template
        )

        if output_format == "html":
            return HttpResponse(rendered_html, content_type="text/html; charset=utf-8")

        try:
            pdf_bytes = PDFService.html_to_pdf(rendered_html)
        except RuntimeError as exc:
            return HttpResponse(str(exc), status=503, content_type="text/plain; charset=utf-8")

        return self._pdf_response(pdf_bytes)

    @staticmethod
    def _error(message: str, status: int, output_format: str):
        if output_format == "html":
            return HttpResponse(message, status=status, content_type="text/plain; charset=utf-8")
        return JsonResponse({"error": message}, status=status)
