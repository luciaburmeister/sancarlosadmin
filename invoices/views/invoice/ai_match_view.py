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

from invoices.models import AIMatchSuggestion, AIMatchSuggestionStatus, Invoice, InvoicePOAllocation
from invoices.services.po_matching import POInvoiceMatchService


class InvoiceAIMatchView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Review screen for AI-suggested PO matches. Unlike InvoiceMatchPOView
    (which locks to one PO chosen up front), each AIMatchSuggestion here
    already carries its own suggested PO — different suggestions on the
    same invoice can point at different POs. Confirm/reject happens per
    suggestion via individual POST forms; unmatch/re-review reuses the
    same allocation-removal flow as the manual matcher.
    """
    template_name = "admin/invoices/invoice/ai_match_review.html"
    title = _("AI Suggested Matches")
    permission_required = ("invoices.add_invoicepoallocation",)

    def get_invoice(self):
        return get_object_or_404(Invoice, pk=self.kwargs["object_id"])

    def get_pending_suggestions(self, invoice):
        return (
            AIMatchSuggestion.objects
            .filter(invoice=invoice, status=AIMatchSuggestionStatus.PENDING)
            .select_related(
                "invoice_item",
                "po_item__purchase_order",
                "po_item__inventory_item",
            )
            .order_by("invoice_item_id")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice = self.get_invoice()
        suggestions = self.get_pending_suggestions(invoice)

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
        action = request.POST.get("action")

        if action == "unmatch":
            return self._handle_unmatch(request, invoice)

        if action == "confirm_all":
            return self._handle_confirm_all(request, invoice)

        suggestion_id = request.POST.get("suggestion_id")
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

    def _handle_confirm_all(self, request, invoice):
        """
        Confirms every PENDING suggestion for this invoice in one go, using
        whatever quantity is currently in each row's input (see the
        "Approve All" button in ai_match_review.html, which collects those
        values via JS before submitting) — falling back to the AI's own
        suggested quantity for any row the JS didn't send. Mirror of
        POAIMatchView._handle equivalent branch on the PO side.
        """
        suggestions = list(self.get_pending_suggestions(invoice))
        quantity_by_suggestion_id = {
            suggestion.pk: request.POST.get(f"quantity_{suggestion.pk}")
            for suggestion in suggestions
        }
        confirmed_count, errors = POInvoiceMatchService.confirm_suggestions(
            suggestions, quantity_by_suggestion_id, request.user
        )
        if confirmed_count:
            messages.success(
                request,
                _("%(count)d match(es) confirmed.") % {"count": confirmed_count},
            )
        for suggestion, error_message in errors:
            messages.error(
                request,
                _("Could not confirm \"%(description)s\": %(error)s")
                % {"description": suggestion.invoice_item.description, "error": error_message},
            )
        return redirect(request.path)

    def _handle_unmatch(self, request, invoice):
        """
        Removes a single InvoicePOAllocation and stays on this same page so
        the user can immediately pick a new match — whether that match
        originally came from AI or the manual matcher (mirrors
        InvoiceMatchPOView._handle_unmatch).
        """
        allocation_id = request.POST.get("allocation_id")
        allocation = get_object_or_404(
            InvoicePOAllocation.objects.select_related("invoice_item", "po_item__purchase_order"),
            pk=allocation_id,
            invoice_item__invoice=invoice,
        )
        po_number = allocation.po_item.purchase_order.number
        description = allocation.invoice_item.description
        allocation.delete()
        messages.info(
            request,
            _("Removed match for \"%(description)s\" (was %(po)s). Run the AI matcher again or use the manual matcher to pick a new one.")
            % {"description": description, "po": po_number},
        )
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