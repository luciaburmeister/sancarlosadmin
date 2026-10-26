"""
Public documents behind an asset transaction's QR code (no login), and
behind each DI's own QR once a receipt has been split into several
(AssetTransactionReceipt.split_into_parts) -- same mechanics either way,
only which model/template-lookup/filename/task differs (see subclasses).

The document is stored on the object itself (document_file) and the token
URL just serves that file; saving re-renders it in the background, and the
previous file is served until the new one is stored. The first request
queues the render on the pdf Celery queue and answers with a small
standalone page that polls, then reloads the same URL.
"""
from celery.result import AsyncResult
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic.detail import SingleObjectMixin

from assets.documents import (
    document_filename,
    get_document_template,
    pause_document_filename,
    queue_document_generation,
    queue_pause_generation,
    queue_receipt_generation,
    receipt_document_filename,
)
from assets.models import AssetTransaction, AssetTransactionPause, AssetTransactionReceipt


class _PublicDocumentView(SingleObjectMixin, View):
    """Shared "serve the stored file, or queue a render and show a polling
    page" logic. Subclasses fill in the pieces that differ between an
    AssetTransaction and one of its DI receipts."""

    slug_field = "public_token"
    slug_url_kwarg = "token"
    not_available_message = _("Document is not available.")
    status_url_name = ""

    def template_lookup_transaction(self, obj):
        """The AssetTransaction whose company template renders this object."""
        raise NotImplementedError

    def filename(self, obj):
        raise NotImplementedError

    def queue(self, obj):
        raise NotImplementedError

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.document_file:
            return self.serve(obj)
        if get_document_template(self.template_lookup_transaction(obj)) is None:
            raise Http404(self.not_available_message)

        task_id = self.queue(obj)
        status_url = reverse(self.status_url_name, args=[obj.public_token])
        return render(request, "assets/transaction_document_status.html", {
            "poll_url": f"{status_url}?task_id={task_id}",
            "document_url": request.path,
        })

    def serve(self, obj):
        response = FileResponse(obj.document_file.open("rb"), content_type="application/pdf")
        response["Content-Disposition"] = content_disposition_header(False, self.filename(obj))
        return response


class _PublicDocumentStatusView(SingleObjectMixin, View):
    slug_field = "public_token"
    slug_url_kwarg = "token"

    def get(self, request, *args, **kwargs):
        obj = self.get_object()
        if obj.document_file:
            return JsonResponse({"state": "done"})

        result = AsyncResult(request.GET.get("task_id", ""))
        if result.failed() or (result.successful() and (result.result or {}).get("status") != "ok"):
            return JsonResponse({"state": "error", "message": str(_("The document could not be prepared."))})
        return JsonResponse({"state": "running"})


class AssetTransactionDocumentView(_PublicDocumentView):
    model = AssetTransaction
    status_url_name = "asset_transaction_document_status"
    not_available_message = _("Asset transaction document is not available.")

    def get_queryset(self):
        return super().get_queryset().select_related("company")

    def template_lookup_transaction(self, obj):
        return obj

    def filename(self, obj):
        return document_filename(obj)

    def queue(self, obj):
        return queue_document_generation(obj.pk)


class AssetTransactionDocumentStatusView(_PublicDocumentStatusView):
    model = AssetTransaction


class AssetTransactionReceiptDocumentView(_PublicDocumentView):
    model = AssetTransactionReceipt
    status_url_name = "asset_transaction_receipt_document_status"
    not_available_message = _("DI document is not available.")

    def get_queryset(self):
        return super().get_queryset().select_related("transaction__company")

    def template_lookup_transaction(self, obj):
        return obj.transaction

    def filename(self, obj):
        return receipt_document_filename(obj)

    def queue(self, obj):
        return queue_receipt_generation(obj.pk)


class AssetTransactionReceiptDocumentStatusView(_PublicDocumentStatusView):
    model = AssetTransactionReceipt


class AssetTransactionPauseDocumentView(_PublicDocumentView):
    model = AssetTransactionPause
    status_url_name = "asset_transaction_pause_document_status"
    not_available_message = _("Pause document is not available.")

    def get_queryset(self):
        return super().get_queryset().select_related("movement__company")

    def template_lookup_transaction(self, obj):
        return obj.movement

    def filename(self, obj):
        return pause_document_filename(obj)

    def queue(self, obj):
        return queue_pause_generation(obj.pk)


class AssetTransactionPauseDocumentStatusView(_PublicDocumentStatusView):
    model = AssetTransactionPause
