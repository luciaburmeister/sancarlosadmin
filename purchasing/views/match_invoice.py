from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin
from django.views import View

from purchasing.models import PurchaseOrder, PurchaseOrderStatus
from django.utils import timezone 


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
            "pos": [po],
            "ids_param": str(po.pk),
            "cancel_url": reverse("admin:purchasing_purchaseorder_change", args=[po.pk]),
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
            for po_item, pending in POInvoiceMatchService.po_items_with_pending(
                po, for_credit=invoice.is_credit_note
            ):
                suggestion = suggestions.get(po_item.pk, {})
                allocations = list(po_item.invoice_allocations.all())
                is_matched = pending <= 0
                pending_amount = po_item.unit_price * pending if po_item.unit_price is not None else None
                if not allocations:
                    matched_summary = None
                elif len(allocations) == 1:
                    matched_summary = allocations[0].invoice_item.description
                else:
                    matched_summary = _("%(count)d invoice lines") % {"count": len(allocations)}
                rows.append({
                    "po": po,
                    "item": po_item,
                    "pending": pending,
                    "is_matched": is_matched,
                    "pending_amount": pending_amount,
                    "gap_amount": pending_amount,
                    "line_amount": po_item.unit_price * po_item.quantity if po_item.unit_price is not None else None,
                    "allocations": allocations,
                    "matched_amount": po_item.unit_price * po_item.invoiced_quantity if po_item.unit_price is not None else None,
                    "matched_summary": matched_summary,
                    "suggested_target": suggestion.get("target_id"),
                    "suggested_qty": suggestion.get("quantity") or "",
                })
            context["rows"] = rows
            context["target_lines"] = POInvoiceMatchService.invoice_items_with_remaining(invoice)
            currency = po.total_amount.currency if po.total_amount else "EUR"
            context["progress"] = POInvoiceMatchService.progress_summary(rows, currency)
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
        for po_item, pending in POInvoiceMatchService.po_items_with_pending(
            po, for_credit=invoice.is_credit_note
        ):
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

class BulkPOMatchInvoiceView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Same guided flow as POMatchInvoiceView, but against several purchase
    orders at once — reached from the changelist's "Match selected Purchase
    Orders to an invoice" bulk action — so a provider who splits one invoice
    across many small POs doesn't have to be matched one PO at a time.
    """
    template_name = "purchasing/match_invoice.html"
    title = _("Match Invoice to Selected Purchase Orders")
    permission_required = ("invoices.add_invoicepoallocation",)

    def get_pos(self):
        raw_ids = self.request.GET.get("ids") or self.request.POST.get("ids") or ""
        try:
            ids = [int(v) for v in raw_ids.split(",") if v]
        except ValueError:
            ids = []
        if not ids:
            return []
        return list(
            PurchaseOrder.objects.filter(
                pk__in=ids,
                status__in=[PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CLOSED],
            ).select_related("provider", "company").order_by("date_ordered")
        )

    def get_selected_invoice(self):
        invoice_id = self.request.GET.get("invoice") or self.request.POST.get("invoice")
        if not invoice_id:
            return None
        from invoices.models import Invoice
        return get_object_or_404(Invoice, pk=invoice_id)

    def dispatch(self, request, *args, **kwargs):
        pos = self.get_pos()
        if not pos:
            messages.error(request, _("Select at least one approved or closed purchase order to match."))
            return redirect("admin:purchasing_purchaseorder_changelist")
        if len({po.provider_id for po in pos}) > 1 or len({po.company_id for po in pos}) > 1:
            messages.error(
                request,
                _("Select purchase orders from a single provider and company to match them to one invoice."),
            )
            return redirect("admin:purchasing_purchaseorder_changelist")
        self._pos = pos
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from invoices.services.po_matching import POInvoiceMatchService

        context = super().get_context_data(**kwargs)
        pos = self._pos
        invoice = self.get_selected_invoice()
        context.update({
            "pos": pos,
            "is_bulk": True,
            "ids_param": ",".join(str(po.pk) for po in pos),
            "cancel_url": reverse("admin:purchasing_purchaseorder_changelist"),
            "selected_invoice": invoice,
            "opts": PurchaseOrder._meta,
        })
        if invoice is None:
            candidates = POInvoiceMatchService.candidate_invoices_for_pos(pos)
            for candidate in candidates:
                candidate.items_with_remaining = POInvoiceMatchService.invoice_items_with_remaining(candidate)
            context["candidates"] = candidates
        else:
            suggestions = POInvoiceMatchService.suggest_pairs_for_pos(pos, invoice)
            rows = []
            for po in pos:
                for po_item, pending in POInvoiceMatchService.po_items_with_pending(
                    po, for_credit=invoice.is_credit_note
                ):
                    suggestion = suggestions.get(po_item.pk, {})
                    allocations = list(po_item.invoice_allocations.all())
                    is_matched = pending <= 0
                    pending_amount = po_item.unit_price * pending if po_item.unit_price is not None else None
                    if not allocations:
                        matched_summary = None
                    elif len(allocations) == 1:
                        matched_summary = allocations[0].invoice_item.description
                    else:
                        matched_summary = _("%(count)d invoice lines") % {"count": len(allocations)}
                    rows.append({
                        "po": po,
                        "item": po_item,
                        "pending": pending,
                        "is_matched": is_matched,
                        "pending_amount": pending_amount,
                        "gap_amount": pending_amount,
                        "line_amount": po_item.unit_price * po_item.quantity if po_item.unit_price is not None else None,
                        "allocations": allocations,
                        "matched_amount": po_item.unit_price * po_item.invoiced_quantity if po_item.unit_price is not None else None,
                        "matched_summary": matched_summary,
                        "suggested_target": suggestion.get("target_id"),
                        "suggested_qty": suggestion.get("quantity") or "",
                    })
            context["rows"] = rows
            context["target_lines"] = POInvoiceMatchService.invoice_items_with_remaining(invoice)
            currency = pos[0].total_amount.currency if pos[0].total_amount else "EUR"
            context["progress"] = POInvoiceMatchService.progress_summary(rows, currency)
        return context

    def post(self, request, *args, **kwargs):
        from invoices.services.po_matching import POInvoiceMatchService

        pos = self._pos
        ids_param = ",".join(str(po.pk) for po in pos)
        invoice = self.get_selected_invoice()
        if invoice is None:
            messages.error(request, _("No invoice was selected."))
            return redirect(f"{request.path}?ids={ids_param}")

        invoice_items = {
            item.pk: item
            for item, _ in POInvoiceMatchService.invoice_items_with_remaining(invoice)
        }
        pairs = []
        for po in pos:
            for po_item, pending in POInvoiceMatchService.po_items_with_pending(
                po, for_credit=invoice.is_credit_note
            ):
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
            return redirect(f"{request.path}?ids={ids_param}&invoice={invoice.pk}")

        try:
            created = POInvoiceMatchService.confirm(pairs)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect(f"{request.path}?ids={ids_param}&invoice={invoice.pk}")

        messages.success(
            request,
            _("%(count)d line(s) across %(po_count)d purchase order(s) matched to invoice %(number)s.")
            % {"count": len(created), "po_count": len(pos), "number": invoice.invoice_number or invoice.pk},
        )
        return redirect("admin:purchasing_purchaseorder_changelist")


class POAIMatchView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Review screen for AI-suggested invoice matches, from the PO side.
    Mirror of invoices.views.invoice.ai_match_view.InvoiceAIMatchView:
    each AIMatchSuggestion here already carries its own matched invoice —
    different suggestions for the same PO can point at different invoices,
    since a single PO's lines are often billed across several invoices
    over time. Confirm/reject/unmatch reuse the exact same
    POInvoiceMatchService.confirm() flow as the manual matcher, so a match
    made here is indistinguishable from one made through POMatchInvoiceView.
    """
    template_name = "purchasing/ai_match_invoice.html"
    title = _("AI Suggested Matches")
    permission_required = ("invoices.add_invoicepoallocation",)

    def get_po(self):
        return get_object_or_404(PurchaseOrder, pk=self.kwargs["po_id"])

    def get_pending_suggestions(self, po):
        from invoices.models import AIMatchSuggestion, AIMatchSuggestionStatus

        return (
            AIMatchSuggestion.objects
            .filter(po_item__purchase_order=po, status=AIMatchSuggestionStatus.PENDING)
            .select_related(
                "invoice_item__invoice",
                "po_item",
            )
            .order_by("po_item_id")
        )

    def get_context_data(self, **kwargs):
        from invoices.services.po_matching import POInvoiceMatchService

        context = super().get_context_data(**kwargs)
        po = self.get_po()
        suggestions = self.get_pending_suggestions(po)

        context.update({
            "po": po,
            "suggestions": suggestions,
            "opts": PurchaseOrder._meta,
        })
        context.update(POInvoiceMatchService.match_progress_for_po(po))
        return context

    def post(self, request, *args, **kwargs):
        from invoices.models import AIMatchSuggestion, AIMatchSuggestionStatus, InvoicePOAllocation
        from invoices.services.po_matching import POInvoiceMatchService

        po = self.get_po()
        action = request.POST.get("action")

        if action == "unmatch":
            allocation_id = request.POST.get("allocation_id")
            allocation = get_object_or_404(
                InvoicePOAllocation.objects.select_related("invoice_item__invoice", "po_item"),
                pk=allocation_id,
                po_item__purchase_order=po,
            )
            invoice_number = allocation.invoice_item.invoice.invoice_number or str(allocation.invoice_item.invoice.pk)
            description = allocation.po_item.inventory_item.name if allocation.po_item.inventory_item_id else allocation.po_item.description
            allocation.delete()
            messages.info(
                request,
                _("Removed match for \"%(description)s\" (was invoice %(invoice)s). Choose a new match below.")
                % {"description": description, "invoice": invoice_number},
            )
            return redirect(request.path)

        if action == "confirm_all":
            suggestions = list(self.get_pending_suggestions(po))
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
                description = (
                    suggestion.po_item.inventory_item.name
                    if suggestion.po_item.inventory_item_id
                    else suggestion.po_item.description
                )
                messages.error(
                    request,
                    _("Could not confirm \"%(description)s\": %(error)s")
                    % {"description": description, "error": error_message},
                )
            return redirect(request.path)

        suggestion_id = request.POST.get("suggestion_id")
        suggestion = get_object_or_404(
            AIMatchSuggestion,
            pk=suggestion_id,
            po_item__purchase_order=po,
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
    
class DispatchPOAIMatchView(UnfoldModelAdminViewMixin, View):
    """
    Queues suggest_ai_invoice_matches_task and redirects to the shared
    Celery status page. Mirror of InvoiceAdmin._dispatch_ai_match — kept as
    its own view (rather than an admin action) since PurchaseOrderAdmin's
    "Match Invoice" button already lives inside match_invoice.html rather
    than as a bare admin action, and the "Try AI Suggest Match" link there
    needs a URL to point at directly.
    """
    permission_required = ("invoices.add_invoicepoallocation",)

    def get(self, request, po_id):
        from invoices.tasks import suggest_ai_invoice_matches_task
        from purchasing.models import PurchaseOrder

        po = get_object_or_404(PurchaseOrder, pk=po_id)

        result = suggest_ai_invoice_matches_task.delay(po.pk)
        back_url = reverse("admin:purchasing_purchaseorder_ai_match_invoice", args=[po.pk])
        status_url = (
            reverse("admin:banking_matcher_status")
            + f"?task_id={result.id}&task_type=ai_invoice_match&back_url={back_url}"
        )
        return redirect(status_url)