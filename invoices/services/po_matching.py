"""
Suggest-then-confirm matching between purchase invoices and purchase orders.

Same human-in-the-loop flow as banking's InvoiceMatcher, but computed live:
candidate scoring here is cheap, so there is no staging model and no async task.
Confirming creates InvoicePOAllocation rows; all business validation
(over-allocation, provider/company/status) lives on the model itself.
"""
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from djmoney.money import Money

from invoices.models import AIMatchSuggestionStatus, Invoice, InvoicePOAllocation


def _tokens(text):
    return set(re.findall(r"[0-9a-zñáéíóúü]+", (text or "").lower()))


class POInvoiceMatchService:
    MAX_CANDIDATES = 30
    # Candidates whose date is further than this from the document being
    # matched are not suggested — an invoice rarely bills a PO that old.
    MAX_DATE_DELTA_DAYS = 90

    # ------------------------------------------------------------------
    # Candidates (step 1 of the wizard)
    # ------------------------------------------------------------------

    @classmethod
    def candidate_pos_for_invoice(cls, invoice):
        """
        POs this invoice could be billing: same company/provider, invoiceable
        status, with quantity still pending to invoice. Sorted by how close the
        PO total is to the invoice base.
        """
        from purchasing.models import PurchaseOrder, PurchaseOrderStatus

        qs = PurchaseOrder.objects.filter(
            status__in=[PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CLOSED],
        )
        if invoice.company_id:
            qs = qs.filter(company_id=invoice.company_id)
        if invoice.provider_id:
            qs = qs.filter(provider_id=invoice.provider_id)
        if invoice.date_issued:
            delta = timedelta(days=cls.MAX_DATE_DELTA_DAYS)
            qs = qs.filter(
                date_ordered__gte=invoice.date_issued - delta,
                date_ordered__lte=invoice.date_issued + delta,
            )
        qs = qs.annotate(
            _ordered=Sum("items__quantity"),
            _invoiced=Sum("items__invoiced_quantity"),
        )
        if invoice.is_credit_note:
            # A credit note reverses billed quantity, so only POs with something
            # already invoiced are creditable.
            qs = qs.filter(_invoiced__gt=0)
        else:
            qs = qs.filter(_ordered__gt=F("_invoiced"))
        qs = qs.select_related("provider").order_by("-date_ordered")

        pos = list(qs[:50])
        for po in pos:
            if invoice.is_credit_note:
                po.pending_to_invoice = po._invoiced or Decimal("0")
            else:
                po.pending_to_invoice = (po._ordered or Decimal("0")) - (po._invoiced or Decimal("0"))

        target = cls._invoice_base(invoice)
        if target is not None:
            # abs() so a credit note's negative base compares against PO totals.
            target = abs(target)
            pos.sort(key=lambda po: abs(cls._money_amount(po.total_amount) - target))
        return pos[: cls.MAX_CANDIDATES]

    @classmethod
    def candidate_invoices_for_po(cls, po):
        """
        Purchase invoices that could bill this PO: same company, same provider
        (or provider not yet identified), with at least one line not fully
        allocated. Sorted by how close the invoice base is to the PO total.
        """
        qs = (
            Invoice.objects.filter(company_id=po.company_id)
            .filter(Q(provider_id=po.provider_id) | Q(provider__isnull=True))
            .select_related("provider")
            .prefetch_related("items__po_allocations")
            .order_by("-date_issued")
        )
        if po.date_ordered:
            delta = timedelta(days=cls.MAX_DATE_DELTA_DAYS)
            qs = qs.filter(
                # Invoices still awaiting OCR have no issue date yet — keep them.
                Q(date_issued__isnull=True)
                | Q(
                    date_issued__gte=po.date_ordered - delta,
                    date_issued__lte=po.date_ordered + delta,
                )
            )

        result = []
        for inv in qs[:100]:
            unmatched = [r for _, r in cls.invoice_items_with_remaining(inv) if r > 0]
            if unmatched:
                inv.unmatched_lines = len(unmatched)
                result.append(inv)

        target = cls._money_amount(po.total_amount)
        result.sort(key=lambda inv: abs(abs(cls._invoice_base(inv) or target) - target))
        return result[: cls.MAX_CANDIDATES]

    # ------------------------------------------------------------------
    # Line-level data and suggestions (step 2 prefill)
    # ------------------------------------------------------------------

    @staticmethod
    def invoice_items_with_remaining(invoice):
        """Returns [(InvoiceItem, remaining_qty)] — remaining = qty minus already allocated."""
        out = []
        for item in invoice.items.all():
            allocated = sum((a.quantity for a in item.po_allocations.all()), Decimal("0"))
            out.append((item, item.quantity - allocated))
        return out

    @staticmethod
    def po_items_with_pending(po, for_credit=False):
        """
        Returns [(PurchaseOrderItem, pending_invoice_quantity)].
        For credit notes the consumable pool is what has already been invoiced
        (the creditable quantity), not what remains to invoice.
        """
        return [
            (pi, pi.invoiced_quantity if for_credit else pi.pending_invoice_quantity)
            for pi in po.items.select_related("inventory_item").prefetch_related(
                "invoice_allocations__invoice_item__invoice"
            )
        ]

    @classmethod
    def suggest_pairs_for_invoice(cls, invoice, po):
        """
        For each invoice line with remaining qty, suggest the best PO line.
        Returns {invoice_item_id: {"target_id": po_item_id, "quantity": qty}}.
        """
        sources = [
            (item, remaining, item.description, item.unit_price)
            for item, remaining in cls.invoice_items_with_remaining(invoice)
        ]
        targets = [
            (pi, pending, cls._po_item_text(pi), pi.unit_price)
            for pi, pending in cls.po_items_with_pending(po, for_credit=invoice.is_credit_note)
        ]
        return cls._suggest(sources, targets)

    @classmethod
    def suggest_pairs_for_po(cls, po, invoice):
        """
        For each PO line with pending qty, suggest the best invoice line.
        Returns {po_item_id: {"target_id": invoice_item_id, "quantity": qty}}.
        """
        sources = [
            (pi, pending, cls._po_item_text(pi), pi.unit_price)
            for pi, pending in cls.po_items_with_pending(po, for_credit=invoice.is_credit_note)
        ]
        targets = [
            (item, remaining, item.description, item.unit_price)
            for item, remaining in cls.invoice_items_with_remaining(invoice)
        ]
        return cls._suggest(sources, targets)

    # ------------------------------------------------------------------
    # Review-step progress (checklist header)
    # ------------------------------------------------------------------

    @staticmethod
    def progress_summary(rows, currency="EUR"):
        """
        rows: dicts with "is_matched" (bool), "matched_amount" (Money|None) and
        "gap_amount" (Money|None, the not-yet-matched amount for that line).
        Used to render the "N of M lines matched" checklist header.
        """
        total_count = len(rows)
        matched_count = sum(1 for r in rows if r["is_matched"])
        matched_total = Decimal("0")
        attention_total = Decimal("0")
        for r in rows:
            if r.get("matched_amount") is not None:
                matched_total += r["matched_amount"].amount
            if not r["is_matched"] and r.get("gap_amount") is not None:
                attention_total += r["gap_amount"].amount
        return {
            "matched_count": matched_count,
            "total_count": total_count,
            "unresolved_count": total_count - matched_count,
            "percent": round(matched_count / total_count * 100) if total_count else 0,
            "matched_total": Money(matched_total, currency),
            "attention_total": Money(attention_total, currency),
        }

    @classmethod
    def match_progress(cls, invoice):
        """
        Quantity-based progress summary + already-matched breakdown, shared
        by both the manual matcher (match_po.html) and the AI matcher
        (ai_match_review.html) so both screens present the same "what's
        already matched" picture regardless of which matcher was used.
        `allocation_objs` carries the real InvoicePOAllocation rows so the
        template can offer an "Unmatch" action per allocation.

        Returns:
        {
            "matched_quantity": Decimal,
            "total_quantity": Decimal,
            "item_progress": [{"description", "matched", "total"}, ...]   # lines still pending
            "matched_items": [{"description", "matched", "total", "is_complete",
                                "allocation_objs": [InvoicePOAllocation, ...],
                                "allocations": [{"po_number", "quantity", "from_ai"}, ...]}, ...]
        }
        """
        items_with_remaining = cls.invoice_items_with_remaining(invoice)
        total_quantity = sum((item.quantity for item, _ in items_with_remaining), Decimal("0"))
        remaining_quantity = sum((remaining for _, remaining in items_with_remaining), Decimal("0"))
        matched_quantity = total_quantity - remaining_quantity

        item_progress = [
            {
                "description": item.description,
                "matched": item.quantity - remaining,
                "total": item.quantity,
            }
            for item, remaining in items_with_remaining
            if remaining > 0
        ]

        matched_items = []
        for item, remaining in items_with_remaining:
            allocations = list(item.po_allocations.select_related("po_item__purchase_order").all())
            if not allocations:
                continue
            matched_items.append({
                "description": item.description,
                "matched": item.quantity - remaining,
                "total": item.quantity,
                "is_complete": remaining <= 0,
                "allocation_objs": allocations,
                "allocations": [
                    {
                        "po_number": alloc.po_item.purchase_order.number,
                        "quantity": alloc.quantity,
                        "from_ai": alloc.gemini_match_response is not None,
                    }
                    for alloc in allocations
                ],
            })

        return {
            "matched_quantity": matched_quantity,
            "total_quantity": total_quantity,
            "item_progress": item_progress,
            "matched_items": matched_items,
        }

    @classmethod
    def match_progress_for_po(cls, po):
        """
        Mirror of match_progress(), built from the PO side: progress is
        measured over this PO's own lines instead of an invoice's lines.
        Used by POAIMatchView (and could be reused by the manual
        POMatchInvoiceView) so both screens present the same "what's
        already matched" picture for a PO regardless of which matcher
        confirmed it.
        """
        items_with_pending = cls.po_items_with_pending(po)
        total_quantity = sum((item.quantity for item, _ in items_with_pending), Decimal("0"))
        pending_quantity = sum((pending for _, pending in items_with_pending), Decimal("0"))
        matched_quantity = total_quantity - pending_quantity

        item_progress = [
            {
                "description": item.inventory_item.name if item.inventory_item_id else item.description,
                "matched": item.quantity - pending,
                "total": item.quantity,
            }
            for item, pending in items_with_pending
            if pending > 0
        ]

        matched_items = []
        for item, pending in items_with_pending:
            allocations = list(item.invoice_allocations.select_related("invoice_item__invoice").all())
            if not allocations:
                continue
            matched_items.append({
                "description": item.inventory_item.name if item.inventory_item_id else item.description,
                "matched": item.quantity - pending,
                "total": item.quantity,
                "is_complete": pending <= 0,
                "allocation_objs": allocations,
                "allocations": [
                    {
                        "invoice_number": alloc.invoice_item.invoice.invoice_number or str(alloc.invoice_item.invoice.pk),
                        "invoice_pk": alloc.invoice_item.invoice.pk,
                        "quantity": alloc.quantity,
                        "from_ai": alloc.gemini_match_response is not None,
                    }
                    for alloc in allocations
                ],
            })

        return {
            "matched_quantity": matched_quantity,
            "total_quantity": total_quantity,
            "item_progress": item_progress,
            "matched_items": matched_items,
        }

    # ------------------------------------------------------------------
    # AI matcher pool (data assembly for GeminiPOItemMatcher)
    # ------------------------------------------------------------------

    @classmethod
    def ai_match_pool(cls, invoice):
        """
        Builds the two data structures fed to GeminiPOItemMatcher: invoice
        lines still without a full match, and candidate PO lines grouped by
        PO, flagging POs already used elsewhere on this same invoice — a
        strong prior for providers billed repeatedly with near-identical POs.
        """
        invoice_items_data = []
        for item, remaining in cls.invoice_items_with_remaining(invoice):
            if remaining <= 0:
                continue
            invoice_items_data.append({
                "id": item.pk,
                "description": item.description,
                "quantity": str(remaining),
                "unit_price": str(cls._money_amount(item.unit_price) or ""),
            })

        if not invoice_items_data:
            return [], []

        already_matched_po_ids = set(
            invoice.linked_purchase_orders().values_list("pk", flat=True)
        )

        po_candidates_data = []
        for po in cls.candidate_pos_for_invoice(invoice):
            items_data = [
                {
                    "id": pi.pk,
                    "name": pi.inventory_item.name if pi.inventory_item_id else "",
                    "description": pi.description,
                    "unit_price": str(cls._money_amount(pi.unit_price) or ""),
                    "pending_quantity": str(pending),
                }
                for pi, pending in cls.po_items_with_pending(po, for_credit=invoice.is_credit_note)
                if pending > 0
            ]
            if not items_data:
                continue
            po_candidates_data.append({
                "po_id": po.pk,
                "po_number": po.number,
                "date_ordered": po.date_ordered.isoformat() if po.date_ordered else None,
                "already_matched_in_this_invoice": po.pk in already_matched_po_ids,
                "items": items_data,
            })

        return invoice_items_data, po_candidates_data

    @classmethod
    def ai_match_pool_for_po(cls, po):
        """
        Mirror of ai_match_pool(), built from the PO side: this PO's own
        pending lines are the source, and candidate invoices (grouped, each
        flagged if already linked to this PO) are the targets. Same
        signal priority as the invoice-side pool — price, quantity, and
        prior-match-on-this-PO as a strong prior for providers billed
        repeatedly against near-identical orders.
        """
        po_items_data = [
            {
                "id": pi.pk,
                "name": pi.inventory_item.name if pi.inventory_item_id else "",
                "description": pi.description,
                "unit_price": str(cls._money_amount(pi.unit_price) or ""),
                "quantity": str(pending),
            }
            for pi, pending in cls.po_items_with_pending(po)
            if pending > 0
        ]

        if not po_items_data:
            return [], []

        already_matched_invoice_ids = set(
            Invoice.objects.filter(
                po_allocations__po_item__purchase_order=po
            ).values_list("pk", flat=True)
        )

        invoice_candidates_data = []
        for invoice in cls.candidate_invoices_for_po(po):
            items_data = [
                {
                    "id": item.pk,
                    "description": item.description,
                    "unit_price": str(cls._money_amount(item.unit_price) or ""),
                    "pending_quantity": str(remaining),
                }
                for item, remaining in cls.invoice_items_with_remaining(invoice)
                if remaining > 0
            ]
            if not items_data:
                continue
            invoice_candidates_data.append({
                "invoice_id": invoice.pk,
                "invoice_number": invoice.invoice_number or str(invoice.pk),
                "date_issued": invoice.date_issued.isoformat() if invoice.date_issued else None,
                "already_matched_to_this_po": invoice.pk in already_matched_invoice_ids,
                "items": items_data,
            })

        return po_items_data, invoice_candidates_data

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------

    @staticmethod
    def confirm(pairs):
        """
        Creates or updates one InvoicePOAllocation per pair, atomically. Each
        pair: {"invoice_item": InvoiceItem, "po_item": PurchaseOrderItem, "quantity": Decimal}.

        If an allocation already exists for this exact (invoice_item, po_item)
        pair — e.g. the AI matcher suggests the same PO line again for an
        invoice line that was only partially matched before, since the PO
        being "already matched in this invoice" is itself a signal the
        matcher is told to prefer — its quantity is increased instead of
        violating the uniqueness constraint on (invoice_item, po_item).
        Model validation raises ValidationError and rolls back everything.

        Also auto-links each involved invoice to its matched PO's project_file
        and cost_center, so the Purchase Order's committed cost shows up as an
        actual expense against the right project and cost center as soon as it's
        matched, instead of relying on the invoice being tagged manually or by
        OCR. The project_file is only filled in when empty; the cost_center also
        replaces a value merely inherited from the provider's default, since the
        order is the more specific source — but never one set by hand.
        """
        created = []
        with transaction.atomic():
            for pair in pairs:
                existing = InvoicePOAllocation.objects.filter(
                    invoice_item=pair["invoice_item"],
                    po_item=pair["po_item"],
                ).first()
                if existing:
                    existing.quantity = existing.quantity + pair["quantity"]
                    existing.save()
                    created.append(existing)
                else:
                    alloc = InvoicePOAllocation(
                        invoice_item=pair["invoice_item"],
                        po_item=pair["po_item"],
                        quantity=pair["quantity"],
                    )
                    alloc.save()
                    created.append(alloc)

            linked_invoices = set()
            for alloc in created:
                if alloc.invoice_id in linked_invoices:
                    continue
                linked_invoices.add(alloc.invoice_id)
                invoice = alloc.invoice
                po = alloc.po_item.purchase_order
                dirty = []

                if po.project_file_id and not invoice.project_file_id:
                    invoice.project_file_id = po.project_file_id
                    dirty.append("project_file")

                # The order beats the provider default the invoice may have been
                # created with, but never a cost center someone chose by hand.
                provider_default_id = (
                    invoice.provider.default_cost_center_id if invoice.provider_id else None
                )
                if po.cost_center_id and invoice.cost_center_id in (None, provider_default_id):
                    if invoice.cost_center_id != po.cost_center_id:
                        invoice.cost_center_id = po.cost_center_id
                        dirty.append("cost_center")

                if dirty:
                    invoice.save(update_fields=dirty)
        return created

    @classmethod
    def confirm_suggestions(cls, suggestions, quantity_by_suggestion_id, reviewed_by):
        """
        Confirms an "Approve All" batch of AIMatchSuggestion rows, one at a
        time — each through its own confirm() call, so each gets its own
        transaction.atomic() and an invalid row (e.g. a hand-edited quantity
        that now exceeds the PO line's pending_invoice_quantity) rolls back
        only itself, not the whole batch. The alternative — a single call to
        confirm() with every pair — would roll back every valid row in the
        batch the moment one pair fails, since confirm() wraps its whole list
        in one atomic block.

        quantity_by_suggestion_id: {suggestion.pk: raw_quantity_string_or_None}.
        A missing or blank entry falls back to the suggestion's own
        AI-suggested quantity, so a plain "Approve All" click with nothing
        hand-edited still works.

        Returns (confirmed_count, errors), where errors is a list of
        (suggestion, error_message) tuples for suggestions left PENDING
        because they failed — the caller reports these to the user without
        touching the suggestions that did succeed.
        """
        confirmed_count = 0
        errors = []

        for suggestion in suggestions:
            raw_quantity = quantity_by_suggestion_id.get(suggestion.pk)
            try:
                quantity = (
                    Decimal(str(raw_quantity).replace(",", "."))
                    if raw_quantity not in (None, "")
                    else suggestion.quantity
                )
            except (InvalidOperation, ValueError, AttributeError):
                errors.append((suggestion, _("Invalid quantity.")))
                continue
            if quantity <= 0:
                errors.append((suggestion, _("Quantity must be greater than zero.")))
                continue

            try:
                created = cls.confirm([{
                    "invoice_item": suggestion.invoice_item,
                    "po_item": suggestion.po_item,
                    "quantity": quantity,
                }])
            except ValidationError as e:
                errors.append((suggestion, "; ".join(e.messages)))
                continue

            created[0].gemini_match_response = suggestion.raw_response
            created[0].save(update_fields=["gemini_match_response"])

            suggestion.status = AIMatchSuggestionStatus.CONFIRMED
            suggestion.reviewed_by = reviewed_by
            suggestion.reviewed_at = timezone.now()
            suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            confirmed_count += 1

        return confirmed_count, errors

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @classmethod
    def _suggest(cls, sources, targets):
        """
        Greedy pairing: score every target per source (price match + description
        token overlap), consume pending quantities so two sources don't claim
        the same target quantity twice.
        """
        pending_map = {t.pk: pending for t, pending, _, _ in targets}
        suggestions = {}
        for source, remaining, source_text, source_price in sources:
            if remaining <= 0:
                continue
            best_id, best_score = None, None
            for t, _, t_text, t_price in targets:
                if pending_map[t.pk] <= 0:
                    continue
                score = cls._score(source_text, source_price, t_text, t_price)
                if best_score is None or score > best_score:
                    best_id, best_score = t.pk, score
            if best_id is None:
                continue
            qty = min(remaining, pending_map[best_id])
            if qty <= 0:
                continue
            suggestions[source.pk] = {"target_id": best_id, "quantity": qty}
            pending_map[best_id] -= qty
        return suggestions

    @classmethod
    def _score(cls, source_text, source_price, target_text, target_price):
        score = len(_tokens(source_text) & _tokens(target_text))
        sp = cls._money_amount(source_price)
        tp = cls._money_amount(target_price)
        # abs() so credit-note lines (negative unit price) match PO prices.
        if sp is not None and tp is not None and abs(sp) == abs(tp):
            score += 3
        return score

    @staticmethod
    def _po_item_text(po_item):
        name = po_item.inventory_item.name if po_item.inventory_item_id else ""
        return f"{po_item.description} {name}"

    @staticmethod
    def _money_amount(value):
        if value is None:
            return None
        return Decimal(str(value.amount if hasattr(value, "amount") else value))

    @classmethod
    def _invoice_base(cls, invoice):
        amount = cls._money_amount(invoice.net_amount)
        if amount:
            return amount
        total = invoice.items.aggregate(t=Sum("line_total"))["t"]
        return cls._money_amount(total)