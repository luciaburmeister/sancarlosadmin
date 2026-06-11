"""Reconciliation review view — approve / switch / skip suggestions."""
import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from banking.models import BankReconciliation, ReconciliationStatus
from banking.services.transaction_matching import TransactionMatcher

logger = logging.getLogger(__name__)


class ReviewReconciliationView(UnfoldModelAdminViewMixin, TemplateView):
    """Display a reconciliation suggestion for human review."""

    title = _("Review Reconciliation")
    permission_required = ("banking.change_bankreconciliation",)
    template_name = "admin/banking/review_reconciliation.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        recon = get_object_or_404(
            BankReconciliation.objects.select_related(
                "bank_transaction__company",
                "bank_transaction__bank_account",
            ),
            pk=self.kwargs["pk"],
        )
        txn = recon.bank_transaction

        # Build enriched combinations with resolved document details
        combinations = []
        for idx, combo in enumerate(recon.suggested_allocations or []):
            allocations = combo.get("allocations", [])
            docs = []
            total = Decimal("0")
            for alloc in allocations:
                ct = ContentType.objects.get(model=alloc["content_type"])
                doc = ct.get_object_for_this_type(pk=alloc["object_id"])
                remaining = doc.amount_remaining_decimal
                total += remaining

                # Build admin change URL for the document
                app_label = ct.app_label
                model_name = ct.model
                try:
                    change_url = reverse(
                        f"admin:{app_label}_{model_name}_change",
                        args=[doc.pk],
                    )
                except Exception:
                    change_url = None

                docs.append({
                    "content_type": alloc["content_type"],
                    "object_id": doc.pk,
                    "number": getattr(doc, "invoice_number", str(doc)),
                    "date": getattr(doc, "date_issued", None),
                    "counterparty": str(
                        getattr(doc, "client", None)
                        or getattr(doc, "provider", None)
                        or "—"
                    ),
                    "remaining": remaining,
                    "change_url": change_url,
                })

            combinations.append({
                "index": idx,
                "is_selected": combo.get("is_selected", False),
                "confidence": combo.get("confidence", "none"),
                "docs": docs,
                "total": total,
                "count": len(docs),
            })

        txn_amount = abs(txn.amount.amount)

        context.update({
            "title": self.title,
            "recon": recon,
            "txn": txn,
            "txn_amount": txn_amount,
            "combinations": combinations,
            "combo_count": len(combinations),
            "approve_url": reverse(
                "admin:banking_reconciliation_approve",
                args=[recon.pk],
            ),
            "skip_url": reverse(
                "admin:banking_reconciliation_skip",
                args=[recon.pk],
            ),
            "back_url": reverse("admin:banking_banktransaction_changelist"),
        })
        return context


class ApproveReconciliationView(UnfoldModelAdminViewMixin, View):
    """POST handler — approve the selected combination."""

    title = _("Approve Reconciliation")
    permission_required = ("banking.change_bankreconciliation",)

    def post(self, request, pk):
        recon = get_object_or_404(BankReconciliation, pk=pk)

        if recon.is_confirmed:
            messages.info(request, _("This reconciliation is already confirmed."))
            return redirect(reverse("admin:banking_banktransaction_changelist"))

        # Which combination did the user select?
        selected_idx = int(request.POST.get("selected_combination", 0))
        combos = recon.suggested_allocations or []

        if selected_idx < 0 or selected_idx >= len(combos):
            messages.error(request, _("Invalid combination selected."))
            return redirect(
                reverse("admin:banking_reconciliation_review", args=[pk])
            )

        # Update is_selected flags
        for idx, combo in enumerate(combos):
            combo["is_selected"] = (idx == selected_idx)
        recon.suggested_allocations = combos
        recon.save(update_fields=["suggested_allocations"])

        # Get the allocations for confirmation
        confirmed_allocations = combos[selected_idx].get("allocations", [])

        try:
            TransactionMatcher.confirm(
                reconciliation=recon,
                confirmed_allocations=confirmed_allocations,
                user=request.user,
            )
            messages.success(
                request,
                _("Reconciliation confirmed. Payment created."),
            )
        except Exception as e:
            logger.exception("Error confirming reconciliation %s", pk)
            messages.error(
                request,
                _("Error confirming: %(error)s") % {"error": str(e)},
            )

        return redirect(reverse("admin:banking_banktransaction_changelist"))


class SkipReconciliationView(UnfoldModelAdminViewMixin, View):
    """POST handler — reject/skip a suggestion (deletes unconfirmed recon)."""

    title = _("Skip Reconciliation")
    permission_required = ("banking.change_bankreconciliation",)

    def post(self, request, pk):
        recon = get_object_or_404(BankReconciliation, pk=pk)

        if recon.is_confirmed:
            messages.warning(request, _("Cannot skip an already confirmed reconciliation."))
        else:
            recon.delete()
            messages.info(request, _("Reconciliation suggestion skipped."))

        return redirect(reverse("admin:banking_banktransaction_changelist"))
