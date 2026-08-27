from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from invoices.models import AIMatchSuggestion, AIMatchSuggestionStatus, Invoice
from invoices.services.po_matching import POInvoiceMatchService


class InvoiceAIMatchView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Review screen for AI-suggested PO matches. Unlike InvoiceMatchPOView
    (which locks to one PO chosen up front), each AIMatchSuggestion here
    already carries its own suggested PO — different suggestions on the
    same invoice can point at different POs. Confirm/reject happens per
    suggestion via individual POST forms, not a single wizard submit.
    """
    template_name = "admin/invoices/invoice/ai_match_review.html"
    title = _("AI Suggested Matches")
    permission_required = ("invoices.add_invoicepoallocation",)

    def get_invoice(self):
        return get_object_or_404(Invoice, pk=self.kwargs["object_id"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice = self.get_invoice()
        suggestions = (
            AIMatchSuggestion.objects
            .filter(invoice=invoice, status=AIMatchSuggestionStatus.PENDING)
            .select_related(
                "invoice_item",
                "po_item__purchase_order",
                "po_item__inventory_item",
            )
            .order_by("invoice_item_id")
        )

        context.update({
            "invoice": invoice,
            "suggestions": suggestions,
            "opts": Invoice._meta,
        })
        # Shared with match_po.html so both matchers show the same
        # "already matched" picture regardless of which one was used.
        context.update(POInvoiceMatchService.match_progress(invoice))
        return context

    def post(self, request, *args, **kwargs):
        invoice = self.get_invoice()
        suggestion_id = request.POST.get("suggestion_id")
        action = request.POST.get("action")

        suggestion = get_object_or_404(
            AIMatchSuggestion,
            pk=suggestion_id,
            invoice=invoice,
            status=AIMatchSuggestionStatus.PENDING,
        )

        if action == "reject":
            suggestion.status = AIMatchSuggestionStatus.REJECTED
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            messages.info(request, _("Suggestion rejected."))
            return redirect(request.path)

        if action == "confirm":
            qty_raw = request.POST.get("quantity", "")
            try:
                quantity = Decimal(qty_raw.replace(",", "."))
            except (InvalidOperation, ValueError, AttributeError):
                messages.error(request, _("Invalid quantity."))
                return redirect(request.path)
            if quantity <= 0:
                messages.error(request, _("Quantity must be greater than zero."))
                return redirect(request.path)

            try:
                created = POInvoiceMatchService.confirm([{
                    "invoice_item": suggestion.invoice_item,
                    "po_item": suggestion.po_item,
                    "quantity": quantity,
                }])
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
                return redirect(request.path)

            created[0].gemini_match_response = suggestion.raw_response
            created[0].save(update_fields=["gemini_match_response"])

            suggestion.status = AIMatchSuggestionStatus.CONFIRMED
            suggestion.reviewed_by = request.user
            suggestion.reviewed_at = timezone.now()
            suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at"])

            messages.success(request, _("Match confirmed."))
            return redirect(request.path)

        messages.error(request, _("Unknown action."))
        return redirect(request.path)


class InvoiceToggleSkipPOMatchingView(UnfoldModelAdminViewMixin, View):
    """
    Toggles Invoice.skip_po_matching. Shared button, rendered identically
    in both match_po.html (manual matcher) and ai_match_review.html (AI
    matcher) — this is a property of the invoice, not of either matcher.
    """
    permission_required = ("invoices.change_invoice",)

    def post(self, request, object_id):
        invoice = get_object_or_404(Invoice, pk=object_id)
        invoice.skip_po_matching = not invoice.skip_po_matching
        invoice.save(update_fields=["skip_po_matching"])

        if invoice.skip_po_matching:
            messages.success(request, _("Marked as no relevant PO."))
        else:
            messages.info(request, _("Unmarked — matching is available again."))

        next_url = request.POST.get("next") or reverse(
            "admin:invoices_invoice_change", args=[invoice.pk]
        )
        return redirect(next_url)