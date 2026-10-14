import json
import logging
import uuid

from django.core.cache import cache
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

    GET  preview/<template_id>/            -> inline PDF (format=html: the HTML)
    POST preview/live/  (html, css, ...)   -> 202 {"token": ...}; the PDF is
                                              rendered on the pdf Celery queue and
                                              fetched from ExportTemplatePreviewResultView
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
        """
        Queue a live render on the pdf worker and return its token at once.

        Nothing is rendered here: a heavy template used to hold a gunicorn
        thread and a pooled DB connection for the whole WeasyPrint render.
        Queuing a new preview supersedes the user's previous one, so a backlog
        built up while typing collapses to the latest version.
        """
        from configurator.tasks import preview_latest_key, render_export_template_preview, PREVIEW_CACHE_TTL

        params = {
            "html": request.POST.get("html", ""),
            "css": request.POST.get("css", ""),
            "kind": request.POST.get("kind", "quotation"),
            "product": request.POST.get("product") or None,
            "company": request.POST.get("company") or None,
            "object_id": request.POST.get("object_id") or request.POST.get("config_id") or None,
        }
        token = uuid.uuid4().hex
        cache.set(preview_latest_key(request.user.pk), token, PREVIEW_CACHE_TTL)
        render_export_template_preview.apply_async(
            args=[token, request.user.pk, params], task_id=token,
        )
        return JsonResponse({"token": token}, status=202)

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


class ExportTemplatePreviewResultView(View):
    """
    GET preview/live/<token>/ -> {"status": "pending"}
                                 {"status": "done", "pdf": <base64>, "context": {...}}
                                 {"status": "error", "error": "..."}  (4xx/5xx)
                                 {"status": "superseded"}

    Reads the cache only, so polling costs no render and no DB query beyond
    the admin's session check.
    """

    def get(self, request, token, *args, **kwargs):
        from configurator.tasks import preview_result_key

        if not request.user.has_perm("configurator.view_configurationexporttemplate"):
            return JsonResponse({"error": "Forbidden"}, status=403)

        result = cache.get(preview_result_key(token))
        if result is None:
            return JsonResponse({"status": "pending"})
        if result["user_id"] != request.user.pk:
            raise Http404("Preview not found")

        status = result["status"]
        if status == "done":
            return JsonResponse({
                "status": "done",
                "pdf": result["pdf"],
                "context": json.loads(result["context"]),
            })
        if status == "error":
            return JsonResponse({"status": "error", "error": result["error"]}, status=result["http_status"])
        return JsonResponse({"status": status})
