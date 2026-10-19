"""
Public document behind an asset transaction's QR code (no login).

The document is stored on the transaction (AssetTransaction.document_file) and
the token URL just serves that file; saving the transaction re-renders it in
the background, and the previous file is served until the new one is stored.
The first request queues the render on the pdf Celery queue and answers with
a small standalone page that polls, then reloads the same URL.
"""
from celery.result import AsyncResult
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import content_disposition_header
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic.detail import SingleObjectMixin

from assets.documents import document_filename, get_document_template, queue_generation
from assets.models import AssetTransaction


class AssetTransactionDocumentMixin(SingleObjectMixin):
    """Looks the transaction up by the public token in the URL (404 if unknown)."""

    model = AssetTransaction
    slug_field = "public_token"
    slug_url_kwarg = "token"


class AssetTransactionDocumentView(AssetTransactionDocumentMixin, View):
    def get_queryset(self):
        return super().get_queryset().select_related("company")

    def get(self, request, *args, **kwargs):
        transaction = self.get_object()
        if transaction.document_file:
            return self.serve(transaction)
        if get_document_template(transaction) is None:
            raise Http404(_("Asset transaction document is not available."))

        task_id = queue_generation(transaction.pk)
        status_url = reverse("asset_transaction_document_status", args=[transaction.public_token])
        return render(request, "assets/transaction_document_status.html", {
            "poll_url": f"{status_url}?task_id={task_id}",
            "document_url": request.path,
        })

    def serve(self, transaction):
        response = FileResponse(transaction.document_file.open("rb"), content_type="application/pdf")
        response["Content-Disposition"] = content_disposition_header(False, document_filename(transaction))
        return response


class AssetTransactionDocumentStatusView(AssetTransactionDocumentMixin, View):
    def get(self, request, *args, **kwargs):
        transaction = self.get_object()
        if transaction.document_file:
            return JsonResponse({"state": "done"})

        result = AsyncResult(request.GET.get("task_id", ""))
        if result.failed() or (result.successful() and (result.result or {}).get("status") != "ok"):
            return JsonResponse({"state": "error", "message": str(_("The document could not be prepared."))})
        return JsonResponse({"state": "running"})
