from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from invoices.models import Invoice
from invoices.services.po_matching import POInvoiceMatchService


class InvoiceMatchPOView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Guided matching from the invoice side: pick a candidate PO, review the
    suggested line pairings, confirm to create the allocations.
    """
    template_name = "admin/invoices/invoice/match_po.html"
    title = _("Match Purchase Order")
    permission_required = ("invoices.add_invoicepoallocation",)

    def get_invoice(self):
        return get_object_or_404(
            Invoice.objects.prefetch_related(
                "items__po_allocations__po_item__inventory_item",
                "items__po_allocations__po_item__purchase_order",
            ),
            pk=self.kwargs["object_id"],
        )

    def get_selected_po(self):
        po_id = self.request.GET.get("po") or self.request.POST.get("po")
        if not po_id:
            return None
        PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
        return get_object_or_404(PurchaseOrder, pk=po_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invoice = self.get_invoice()
        po = self.get_selected_po()
        context.update({
            "invoice": invoice,
            "selected_po": po,
            "opts": Invoice._meta,
        })
        # Shared with ai_match_review.html so both matchers show the same
        # "already matched" picture regardless of which one was used.
        match_progress = POInvoiceMatchService.match_progress(invoice)
        context.update(match_progress)
        context["all_matched"] = (
            match_progress["total_quantity"] > 0
            and match_progress["matched_quantity"] >= match_progress["total_quantity"]
        )

        if po is None:
            candidates = POInvoiceMatchService.candidate_pos_for_invoice(invoice)
            for candidate in candidates:
                candidate.items_with_pending = POInvoiceMatchService.po_items_with_pending(
                    candidate, for_credit=invoice.is_credit_note
                )
            context["candidates"] = candidates
        else:
            suggestions = POInvoiceMatchService.suggest_pairs_for_invoice(invoice, po)
            rows = []
            for item, remaining in POInvoiceMatchService.invoice_items_with_remaining(invoice):
                suggestion = suggestions.get(item.pk, {})
                allocations = list(item.po_allocations.all())
                allocated_qty = item.quantity - remaining
                is_matched = remaining <= 0
                remaining_amount = item.unit_price * remaining if item.unit_price is not None else None
                if not allocations:
                    matched_summary = None
                elif len(allocations) == 1:
                    po_item = allocations[0].po_item
                    matched_summary = po_item.inventory_item.name if po_item.inventory_item_id else po_item.description
                else:
                    matched_summary = _("%(count)d PO lines") % {"count": len(allocations)}
                rows.append({
                    "item": item,
                    "remaining": remaining,
                    "is_matched": is_matched,
                    "remaining_amount": remaining_amount,
                    "gap_amount": remaining_amount,
                    "line_amount": item.unit_price * item.quantity if item.unit_price is not None else None,
                    "allocations": allocations,
                    "matched_amount": item.unit_price * allocated_qty if item.unit_price is not None else None,
                    "matched_summary": matched_summary,
                    "suggested_target": suggestion.get("target_id"),
                    "suggested_qty": suggestion.get("quantity") or "",
                })
            context["rows"] = rows
            context["target_lines"] = POInvoiceMatchService.po_items_with_pending(
                po, for_credit=invoice.is_credit_note
            )
            currency = invoice.total_amount.currency if invoice.total_amount else "EUR"
            context["progress"] = POInvoiceMatchService.progress_summary(rows, currency)
        return context

    def post(self, request, *args, **kwargs):
        invoice = self.get_invoice()
        po = self.get_selected_po()
        if po is None:
            messages.error(request, _("No purchase order was selected."))
            return redirect(request.path)

        po_items = {
            pi.pk: pi
            for pi, _ in POInvoiceMatchService.po_items_with_pending(po, for_credit=invoice.is_credit_note)
        }
        pairs = []
        for item, remaining in POInvoiceMatchService.invoice_items_with_remaining(invoice):
            target_raw = request.POST.get(f"target_{item.pk}")
            qty_raw = request.POST.get(f"qty_{item.pk}")
            if not target_raw or not qty_raw:
                continue
            try:
                po_item = po_items[int(target_raw)]
                qty = Decimal(qty_raw.replace(",", "."))
            except (KeyError, ValueError, InvalidOperation):
                continue
            if qty <= 0:
                continue
            pairs.append({"invoice_item": item, "po_item": po_item, "quantity": qty})

        if not pairs:
            messages.warning(request, _("No lines were selected. Nothing was matched."))
            return redirect(f"{request.path}?po={po.pk}")

        try:
            created = POInvoiceMatchService.confirm(pairs)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect(f"{request.path}?po={po.pk}")

        messages.success(
            request,
            _("%(count)d line(s) matched to purchase order %(number)s.")
            % {"count": len(created), "number": po.number},
        )
        return redirect("admin:invoices_invoice_change", invoice.pk)