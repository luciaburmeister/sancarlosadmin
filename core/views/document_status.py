"""
Status page for documents rendered on the pdf queue (see core.documents).

Also serves the bulk invoice ZIPs (invoices.tasks.build_invoice_pdf_zip):
any task whose result is {"status": "ok", "storage_path", "filename",
"content_type", "user_id"} can be polled and downloaded here.
"""
import logging

from celery.result import AsyncResult
from django.contrib import admin
from django.core.files.storage import default_storage
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

logger = logging.getLogger(__name__)


class DocumentStatusPageView(TemplateView):
    template_name = "admin/core/document_status.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task_id = self.request.GET.get("task_id", "")
        context.update(admin.site.each_context(self.request))
        context.update({
            "title": _("Preparing document…"),
            "task_id": task_id,
            "back_url": self.request.GET.get("back_url", ""),
            "poll_url": reverse("core_document_status_api", args=[task_id]),
            "download_url": reverse("core_document_download", args=[task_id]),
        })
        return context


class DocumentStatusAPIView(View):
    def get_download_url(self, task_id):
        return reverse("core_document_download", args=[task_id])

    def get(self, request, task_id):
        result = AsyncResult(task_id)

        if result.state in ("PENDING", "STARTED", "RETRY"):
            return JsonResponse({"state": "running"})

        if result.state == "SUCCESS":
            data = result.result or {}
            if data.get("status") == "ok":
                return JsonResponse({
                    "state": "done",
                    "count": data.get("count", 0),
                    "skipped": data.get("skipped", 0),
                    "filename": data.get("filename", ""),
                    "download_url": self.get_download_url(task_id),
                })
            return JsonResponse({
                "state": "error",
                "message": data.get("message") or str(_("No document could be generated.")),
            })

        error_msg = str(result.result) if result.result else "Unknown error"
        return JsonResponse({"state": "error", "message": error_msg})


class DocumentDownloadView(View):
    def is_allowed(self, request, data) -> bool:
        owner_id = data.get("user_id")
        return not owner_id or owner_id == request.user.id or request.user.is_superuser

    def get(self, request, task_id):
        result = AsyncResult(task_id)
        if result.state != "SUCCESS":
            return HttpResponse("The document is not ready yet.", status=404)

        data = result.result or {}
        if data.get("status") != "ok":
            return HttpResponse(data.get("message") or "Document unavailable.", status=404)

        if not self.is_allowed(request, data):
            return HttpResponseForbidden("Not allowed to download this file.")

        storage_path = data.get("storage_path")
        if not storage_path:
            return HttpResponse("Document file missing.", status=404)

        try:
            with default_storage.open(storage_path, "rb") as fh:
                content = fh.read()
        except Exception:
            logger.exception("Failed to read rendered document %s", storage_path)
            return HttpResponse("The document could not be read.", status=404)

        response = HttpResponse(content, content_type=data.get("content_type") or "application/octet-stream")
        response["Content-Disposition"] = content_disposition_header(
            not data.get("inline", False), data.get("filename") or "document"
        )
        # Served once: the status page navigates here exactly one time.
        _delete_stored_file(storage_path)
        return response


def _delete_stored_file(storage_path: str) -> None:
    try:
        if storage_path and default_storage.exists(storage_path):
            default_storage.delete(storage_path)
    except Exception:
        logger.exception("Failed to delete rendered document %s", storage_path)
