"""
Public document behind an asset transaction's QR code (no login).

The PDF is rendered on the pdf Celery queue like the admin downloads (see
core.documents): the token URL queues the render and answers with a small
standalone page that polls, then opens the PDF. The status and download URLs
are scoped to the token, and a task only answers for the transaction it was
queued for.
"""
from celery.result import AsyncResult
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from assets.documents import render_asset_transaction_document
from assets.models import AssetTransaction
from configurator.models import (
    ConfigurationExportTemplate,
    ExportTemplateKind,
    ExportTemplateSubtype,
)
from core.documents import DocumentRequest, renderer_path
from core.views.document_status import DocumentDownloadView, DocumentStatusAPIView


def _filename(transaction) -> str:
    return f"transaction_{transaction.pk}.pdf"


def _get_transaction(token):
    return get_object_or_404(AssetTransaction, public_token=token)


def asset_transaction_document_view(request, token):
    from core.tasks import render_document_task

    transaction = get_object_or_404(AssetTransaction.objects.select_related("company"), public_token=token)
    template = ConfigurationExportTemplate.get_default_for_product(
        product=None,
        kind=ExportTemplateKind.ASSET_TRANSACTION,
        company=transaction.company,
        subtype=ExportTemplateSubtype.DOCUMENT,
    )
    if template is None:
        raise Http404(_("Asset transaction document is not available."))
    document = DocumentRequest(
        renderer=renderer_path(render_asset_transaction_document),
        kwargs={
            "transaction_id": transaction.pk,
            "template_id": template.pk,
            "document_url": request.build_absolute_uri(request.path),
        },
        filename=_filename(transaction),
        inline=True,
    )
    task_id = render_document_task.delay(**document.as_task_kwargs()).id
    return render(request, "assets/transaction_document_status.html", {
        "poll_url": reverse("asset_transaction_document_status", args=[token, task_id]),
        "download_url": reverse("asset_transaction_document_download", args=[token, task_id]),
    })


class AssetTransactionDocumentStatusView(DocumentStatusAPIView):
    def get(self, request, token, task_id):
        self.token = token
        transaction = _get_transaction(token)
        result = AsyncResult(task_id)
        if result.state == "SUCCESS" and (result.result or {}).get("filename") != _filename(transaction):
            return JsonResponse({"state": "error", "message": str(_("Document not found."))}, status=404)
        return super().get(request, task_id)

    def get_download_url(self, task_id):
        return reverse("asset_transaction_document_download", args=[self.token, task_id])


class AssetTransactionDocumentDownloadView(DocumentDownloadView):
    def get(self, request, token, task_id):
        self.transaction = _get_transaction(token)
        return super().get(request, task_id)

    def is_allowed(self, request, data) -> bool:
        return data.get("filename") == _filename(self.transaction)
