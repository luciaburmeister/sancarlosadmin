"""Views for the async matcher status page — polls Celery task state."""
import logging

from celery.result import AsyncResult
from django.http import JsonResponse
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

logger = logging.getLogger(__name__)


class MatcherStatusPageView(UnfoldModelAdminViewMixin, TemplateView):
    """Render a status page that polls the task result via JS."""

    title = "Running Matcher…"
    permission_required = ()
    template_name = "admin/banking/matcher_status.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        task_id = self.request.GET.get("task_id", "")
        task_type = self.request.GET.get("task_type", "")
        back_url = self.request.GET.get("back_url", "")

        context.update({
            "task_id": task_id,
            "task_type": task_type,
            "back_url": back_url,
            "poll_url": reverse(
                "admin:banking_matcher_status_api",
                args=[task_id],
            ),
        })
        return context


class MatcherTaskStatusAPIView(UnfoldModelAdminViewMixin, View):
    """JSON endpoint polled by the status page JS."""

    title = "Matcher Status API"
    permission_required = ()

    def get(self, request, task_id):
        result = AsyncResult(task_id)
        task_type = request.GET.get("task_type", "")

        if result.state in ("PENDING", "STARTED", "RETRY"):
            return JsonResponse({"state": "running"})

        if result.state == "SUCCESS":
            data = result.result or {}
            status = data.get("status", "")
            redirect_url = ""

            if task_type == "transaction":
                recon_id = data.get("reconciliation_id")
                if status == "done" and data.get("has_match") and recon_id:
                    redirect_url = reverse(
                        "admin:banking_reconciliation_review",
                        args=[recon_id],
                    )
            elif task_type == "ai_po_match":
                invoice_id = data.get("invoice_id")
                if status == "done" and data.get("has_match") and invoice_id:
                    redirect_url = reverse(
                        "admin:invoices_invoice_ai_match",
                        args=[invoice_id],
                    )
            elif task_type == "ai_invoice_match":
                po_id = data.get("po_id")
                if status == "done" and data.get("has_match") and po_id:
                    redirect_url = reverse(
                        "admin:purchasing_purchaseorder_ai_match_invoice",
                        args=[po_id],
                    )
            else:
                # invoice / salesinvoice
                suggestion_id = data.get("suggestion_id")
                if status == "done" and data.get("has_match") and suggestion_id:
                    redirect_url = reverse(
                        "admin:banking_invoicereconciliationsuggestion_review",
                        args=[suggestion_id],
                    )

            return JsonResponse({
                "state": "done",
                "status": status,
                "has_match": data.get("has_match", False),
                "redirect_url": redirect_url,
                "message": data.get("message", ""),
            })

        # FAILURE or REVOKED
        error_msg = str(result.result) if result.result else "Unknown error"
        return JsonResponse({
            "state": "error",
            "message": error_msg,
        })