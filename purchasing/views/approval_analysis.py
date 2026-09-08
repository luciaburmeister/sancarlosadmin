from decimal import Decimal

from django import forms
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from djmoney.money import Money
from unfold.views import UnfoldModelAdminViewMixin
from purchasing.models import PurchaseOrder, PurchaseOrderStatus


class ApprovePurchaseOrderForm(forms.Form):
    pass


class POApprovalAnalysisView(UnfoldModelAdminViewMixin, FormView):
    """
    Single-PO review screen for the pending-approval stage: shows the order's
    own lines plus how it sits against its project file's budget (facturable
    vs. already invoiced vs. already committed in approved POs), so whoever
    approves can see the impact of this specific purchase before deciding.

    Deliberately read-only except for the Approve shortcut, which reuses
    PurchaseOrderAdmin._approve_po() rather than duplicating that logic.
    Reject stays on the existing RejectPurchaseOrderView flow, since it
    requires capturing a reason. Approve needs no input, so the form has
    no fields; it exists to route the POST through the same
    validate/form_valid flow as RejectPurchaseOrderView instead of a
    hand-rolled action check.
    """
    template_name = "purchasing/approval_analysis.html"
    form_class = ApprovePurchaseOrderForm
    title = _("Approval Analysis")
    permission_required = ("purchasing.can_approve_purchase_order",)

    def dispatch(self, request, *args, **kwargs):
        po_id = self.kwargs.get("po_id")
        if not PurchaseOrder.objects.filter(pk=po_id, status=PurchaseOrderStatus.PENDING_APPROVAL).exists():
            from django.contrib import messages
            messages.error(request, _("Only Pending POs can be reviewed here."))
            return redirect("admin:purchasing_purchaseorder_change", po_id)
        return super().dispatch(request, *args, **kwargs)

    def get_po(self):
        return get_object_or_404(
            PurchaseOrder.objects.select_related("provider", "project_file", "requested_by", "cost_center"),
            pk=self.kwargs["po_id"],
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po = self.get_po()

        context.update({
            "po": po,
            "opts": PurchaseOrder._meta,
            "items": po.items.select_related("inventory_item").all(),
            "back_url": reverse("admin:purchasing_purchaseorder_change", args=[po.pk]),
        })

        project_file = po.project_file
        if project_file is not None:
            projected_purchased = project_file.total_purchased + po.total_amount
            margin_before = project_file.total_amount - project_file.total_purchased
            margin_after = project_file.total_amount - projected_purchased

            invoiceable_amount = project_file.total_amount.amount
            primary_quote = self._primary_quote(project_file)
            latest_invoice = self._latest_sales_invoice(project_file)
            effective_source = latest_invoice or project_file.client
            payment_method_mismatch = bool(
                primary_quote
                and (
                    primary_quote.payment_method != effective_source.payment_method
                    or primary_quote.payment_terms_days != effective_source.payment_terms_days
                )
            )
            context.update({
                "project_file": project_file,
                "primary_quote": primary_quote,
                "latest_invoice": latest_invoice,
                "effective_payment_source": effective_source,
                "payment_method_mismatch": payment_method_mismatch,
                "already_collected": self._already_collected(project_file),
                "projected_purchased": projected_purchased,
                "margin_before": margin_before,
                "margin_before_pct": self._pct(margin_before.amount, invoiceable_amount),
                "margin_after": margin_after,
                "margin_after_pct": self._pct(margin_after.amount, invoiceable_amount),
                "other_pending_pos": self._other_pending_pos(po, project_file),
            })

        return context

    @staticmethod
    def _primary_quote(project_file):
        """The quote whose status/payment terms best represent this project file:
        the accepted one if there is any, otherwise the most recent one."""
        from sales.models import Quote, QuoteStatus

        quotes = project_file.quotes.order_by("-date")
        return quotes.filter(status=QuoteStatus.ACCEPTED).first() or quotes.first()

    @staticmethod
    def _latest_sales_invoice(project_file):
        """
        The most recently issued non-cancelled sales invoice for this project
        file, if any. Its payment_method reflects the client's preference at
        actual billing time, which can drift from the quote's snapshot if the
        client's terms changed in between — this is the one that matters for
        approving further cost commitments against this file.
        """
        from django.db.models import Q
        from invoices.models import SalesInvoice, SalesInvoiceStatus

        return (
            SalesInvoice.objects.filter(
                Q(project_file=project_file) | Q(project_files=project_file)
            )
            .exclude(status=SalesInvoiceStatus.CANCELLED)
            .order_by("-date_issued")
            .first()
        )

    @staticmethod
    def _already_collected(project_file):
        from django.db.models import Q
        from invoices.models import SalesInvoice, SalesInvoiceStatus

        invoices = SalesInvoice.objects.filter(
            Q(project_file=project_file) | Q(project_files=project_file),
            status=SalesInvoiceStatus.FINAL,
        ).distinct()
        currency = project_file.total_amount.currency
        return sum((inv.amount_paid for inv in invoices), start=Money(0, currency))

    @staticmethod
    def _pct(part, whole):
        if not whole:
            return None
        return (part / whole * 100).quantize(Decimal("0.1"))

    def _other_pending_pos(self, po, project_file):
        others = (
            PurchaseOrder.objects.filter(
                project_file=project_file,
                status=PurchaseOrderStatus.PENDING_APPROVAL,
            )
            .exclude(pk=po.pk)
            .select_related("provider")
        )
        others = list(others)
        if not others:
            return None
        total = sum((o.total_amount for o in others), start=Money(0, po.total_amount.currency))
        return {
            "orders": others,
            "count": len(others),
            "total": total,
            "search_url": (
                reverse("admin:purchasing_purchaseorder_changelist")
                + f"?q={project_file.reference}"
            ),
        }

    def form_valid(self, form):
        po = self.get_po()
        self.model_admin._approve_po(self.request, po)
        return redirect("admin:purchasing_purchaseorder_changelist")