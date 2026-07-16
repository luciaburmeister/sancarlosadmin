"""Async ZIP status page for bulk invoice PDF downloads."""
import logging

from celery.result import AsyncResult
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from invoices.services.invoice_pdf_download import InvoicePdfDownloadService

logger = logging.getLogger(__name__)


class InvoicePdfZipStatusPageView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Preparing PDF download…"
    permission_required = ()
    template_name = "admin/invoices/pdf_zip_status.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task_id = self.request.GET.get("task_id", "")
        back_url = self.request.GET.get("back_url", "")
        context.update({
            "task_id": task_id,
            "back_url": back_url,
            "poll_url": reverse("admin:invoices_pdf_zip_status_api", args=[task_id]),
            "download_url": reverse("admin:invoices_pdf_zip_download", args=[task_id]),
        })
        return context


class InvoicePdfZipStatusAPIView(UnfoldModelAdminViewMixin, View):
    title = "PDF ZIP Status API"
    permission_required = ()

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
                    "filename": data.get("filename", "facturas.zip"),
                    "download_url": reverse(
                        "admin:invoices_pdf_zip_download", args=[task_id]
                    ),
                })
            return JsonResponse({
                "state": "error",
                "message": data.get("message") or "No PDF files could be collected.",
            })

        error_msg = str(result.result) if result.result else "Unknown error"
        return JsonResponse({"state": "error", "message": error_msg})


class InvoicePdfZipDownloadView(UnfoldModelAdminViewMixin, View):
    title = "Download PDF ZIP"
    permission_required = ()

    def get(self, request, task_id):
        result = AsyncResult(task_id)
        if result.state != "SUCCESS":
            return HttpResponse("ZIP is not ready yet.", status=404)

        data = result.result or {}
        if data.get("status") != "ok":
            return HttpResponse(data.get("message") or "ZIP unavailable.", status=404)

        owner_id = data.get("user_id")
        if owner_id and owner_id != request.user.id and not request.user.is_superuser:
            return HttpResponseForbidden("Not allowed to download this file.")

        storage_path = data.get("storage_path")
        filename = data.get("filename") or "facturas.zip"
        if not storage_path:
            return HttpResponse("ZIP file missing.", status=404)

        try:
            response = InvoicePdfDownloadService.storage_file_response(
                storage_path, filename
            )
        except Exception:
            logger.exception("Failed to read invoice PDF zip %s", storage_path)
            return HttpResponse("ZIP file could not be read.", status=404)

        # Best-effort cleanup after serving once
        InvoicePdfDownloadService.delete_stored_file(storage_path)
        return response


def redirect_to_pdf_zip_status(request, task_id: str, back_url: str):
    from django.shortcuts import redirect
    from urllib.parse import urlencode

    if not back_url or not url_has_allowed_host_and_scheme(back_url, allowed_hosts=None):
        back_url = reverse("admin:index")
    return redirect(
        reverse("admin:invoices_pdf_zip_status")
        + "?"
        + urlencode({"task_id": task_id, "back_url": back_url})
    )
