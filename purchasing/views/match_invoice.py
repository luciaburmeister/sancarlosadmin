from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from purchasing.models import PurchaseOrder, PurchaseOrderStatus


class POMatchInvoiceView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Guided matching from the PO side: pick a candidate purchase invoice,
    review the suggested line pairings, confirm to create the allocations.
    """
    template_name = "purchasing/match_invoice.html"
    title = _("Match Invoice")
    permission_required = ("invoices.add_invoicepoallocation",)

    def dispatch(self, request, *args, **kwargs):
        po_id = self.kwargs.get("po_id")
        if not PurchaseOrder.objects.filter(
            pk=po_id,
            status__in=[PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CLOSED],
        ).exists():
            messages.error(request, _("Only approved or closed purchase orders can be matched to invoices."))
            return redirect("admin:purchasing_purchaseorder_change", po_id)
        return super().dispatch(request, *args, **kwargs)

    def get_po(self):
        return get_object_or_404(PurchaseOrder, pk=self.kwargs["po_id"])

    def get_selected_invoice(self):
        invoice_id = self.request.GET.get("invoice") or self.request.POST.get("invoice")
        if not invoice_id:
            return None
        from invoices.models import Invoice
        return get_object_or_404(Invoice, pk=invoice_id)

    def get_context_data(self, **kwargs):
        from invoices.services.po_matching import POInvoiceMatchService

        context = super().get_context_data(**kwargs)
        po = self.get_po()
        invoice = self.get_selected_invoice()
        context.update({
            "po": po,
            "selected_invoice": invoice,
            "opts": PurchaseOrder._meta,
        })
        if invoice is None:
            candidates = POInvoiceMatchService.candidate_invoices_for_po(po)
            for candidate in candidates:
                candidate.items_with_remaining = POInvoiceMatchService.invoice_items_with_remaining(candidate)
            context["candidates"] = candidates
        else:
            suggestions = POInvoiceMatchService.suggest_pairs_for_po(po, invoice)
            rows = []
            for po_item, pending in POInvoiceMatchService.po_items_with_pending(po):
                suggestion = suggestions.get(po_item.pk, {})
                rows.append({
                    "item": po_item,
                    "pending": pending,
                    "suggested_target": suggestion.get("target_id"),
                    "suggested_qty": suggestion.get("quantity") or "",
                })
            context["rows"] = rows
            context["target_lines"] = POInvoiceMatchService.invoice_items_with_remaining(invoice)
        return context

    def post(self, request, *args, **kwargs):
        from invoices.services.po_matching import POInvoiceMatchService

        po = self.get_po()
        invoice = self.get_selected_invoice()
        if invoice is None:
            messages.error(request, _("No invoice was selected."))
            return redirect(request.path)

        invoice_items = {
            item.pk: item
            for item, _ in POInvoiceMatchService.invoice_items_with_remaining(invoice)
        }
        pairs = []
        for po_item, pending in POInvoiceMatchService.po_items_with_pending(po):
            target_raw = request.POST.get(f"target_{po_item.pk}")
            qty_raw = request.POST.get(f"qty_{po_item.pk}")
            if not target_raw or not qty_raw:
                continue
            try:
                invoice_item = invoice_items[int(target_raw)]
                qty = Decimal(qty_raw.replace(",", "."))
            except (KeyError, ValueError, InvalidOperation):
                continue
            if qty <= 0:
                continue
            pairs.append({"invoice_item": invoice_item, "po_item": po_item, "quantity": qty})

        if not pairs:
            messages.warning(request, _("No lines were selected. Nothing was matched."))
            return redirect(f"{request.path}?invoice={invoice.pk}")

        try:
            created = POInvoiceMatchService.confirm(pairs)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect(f"{request.path}?invoice={invoice.pk}")

        messages.success(
            request,
            _("%(count)d line(s) matched to invoice %(number)s.")
            % {"count": len(created), "number": invoice.invoice_number or invoice.pk},
        )
        return redirect("admin:purchasing_purchaseorder_change", po.pk)
