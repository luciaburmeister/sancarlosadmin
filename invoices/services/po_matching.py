"""
Suggest-then-confirm matching between purchase invoices and purchase orders.

Same human-in-the-loop flow as banking's InvoiceMatcher, but computed live:
candidate scoring here is cheap, so there is no staging model and no async task.
Confirming creates InvoicePOAllocation rows; all business validation
(over-allocation, provider/company/status) lives on the model itself.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.db import transaction
from django.db.models import F, Q, Sum
from djmoney.money import Money

from invoices.models import Invoice, InvoicePOAllocation


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
        qs = (
            qs.annotate(
                _ordered=Sum("items__quantity"),
                _invoiced=Sum("items__invoiced_quantity"),
            )
            .filter(_ordered__gt=F("_invoiced"))
            .select_related("provider")
            .order_by("-date_ordered")
        )

        pos = list(qs[:50])
        for po in pos:
            po.pending_to_invoice = (po._ordered or Decimal("0")) - (po._invoiced or Decimal("0"))

        target = cls._invoice_base(invoice)
        if target is not None:
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
        result.sort(key=lambda inv: abs((cls._invoice_base(inv) or target) - target))
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
    def po_items_with_pending(po):
        """Returns [(PurchaseOrderItem, pending_invoice_quantity)]."""
        return [
            (pi, pi.pending_invoice_quantity)
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
            for pi, pending in cls.po_items_with_pending(po)
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
            for pi, pending in cls.po_items_with_pending(po)
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

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------

    @staticmethod
    def confirm(pairs):
        """
        Creates one InvoicePOAllocation per pair, atomically. Each pair:
        {"invoice_item": InvoiceItem, "po_item": PurchaseOrderItem, "quantity": Decimal}.
        Model validation raises ValidationError and rolls back everything.
        """
        created = []
        with transaction.atomic():
            for pair in pairs:
                alloc = InvoicePOAllocation(
                    invoice_item=pair["invoice_item"],
                    po_item=pair["po_item"],
                    quantity=pair["quantity"],
                )
                alloc.save()
                created.append(alloc)
        return created

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
        if sp is not None and tp is not None and sp == tp:
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
