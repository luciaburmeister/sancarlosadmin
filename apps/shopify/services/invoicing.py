"""
Invoicing a store's web orders, as each store chooses.

``ShopifyStore.invoice_on`` picks the invoicing strategy: by hand (staff
invoice the delivery orders, as for any other order), once paid (one invoice
for the whole order) or once shipped (one invoice per dispatched delivery
order). Invoices start as pro forma; with ``issue_invoices`` they are issued
the way staff issue them, with ``SalesInvoiceService.finalize_invoice``
(number, tax agency submission and, with ``email_invoices``, the client email).
Only orders placed after automatic invoicing was turned on are invoiced, so
nothing is billed twice.

Each Shopify refund is kept as a ``ShopifyRefund``. One made before the goods
were invoiced only takes them off the quote; one made after is settled by the
store's refund strategy (``ShopifyStore.refund_handling``): left for review,
or credited with a credit note priced as the goods were invoiced.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from apps.shopify.enums import OrderFinancialStatus
from apps.shopify.models import (
    InvoiceTiming,
    RefundHandling,
    RefundStatus,
    ShopifyOrder,
    ShopifyRefund,
    ShopifyStore,
    VariantLinkStatus,
)
from apps.shopify.services.base import ShopifyService
from invoices.models import InvoicePaymentStatus, SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus
from invoices.services.sales_invoice_service import CreditNoteLine
from payments.models import PaymentMethod
from sales.models import DELIVERY_ORDER_STOCK_OUT_STATUSES, DeliveryOrder, DeliveryOrderStatus, QuoteStatus
from sales.services.external_order import ExternalOrderError, ExternalOrderService


# ── When web orders are invoiced ──────────────────────────────────────────────


class InvoicingStrategy(ABC):
    """How a store invoices its web orders (``ShopifyStore.invoice_on``)."""

    @abstractmethod
    def invoice(self, store: ShopifyStore, record: ShopifyOrder) -> None:
        """Create the invoices the order is due now."""

    @abstractmethod
    def due(self) -> Q:
        """``ShopifyOrder`` filter of orders this strategy still has to invoice."""


class ManualInvoicing(InvoicingStrategy):
    """Staff invoice the delivery orders by hand, as for any other order."""

    def invoice(self, store: ShopifyStore, record: ShopifyOrder) -> None:
        pass

    def due(self) -> Q:
        return Q(pk__in=[])


class InvoiceOnPayment(InvoicingStrategy):
    """One invoice for the whole order once it is paid, from its expediente."""

    def invoice(self, store: ShopifyStore, record: ShopifyOrder) -> None:
        quote = record.quote
        if record.invoice_id is None:
            if quote.status != QuoteStatus.ACCEPTED:
                return  # not paid yet
            record.invoice = ExternalOrderService.invoice(
                quote, store.issue_invoices, store.email_invoices,
                notes=_("Shopify order %(order)s") % {"order": record.name},
            )
            record.save(update_fields=["invoice"])
        elif record.invoice.status != SalesInvoiceStatus.CANCELLED:
            # Delivery orders made since (e.g. after an order edit) are billed by it too.
            ExternalOrderService.bill_delivery_orders(quote, record.invoice)

    def due(self) -> Q:
        return Q(invoice__isnull=True, quote__status=QuoteStatus.ACCEPTED, is_cancelled=False)


class InvoiceEachShipment(InvoicingStrategy):
    """One invoice per dispatched delivery order; replacements of lost goods are never invoiced."""

    def invoice(self, store: ShopifyStore, record: ShopifyOrder) -> None:
        for delivery_order in self._uninvoiced(ShopifyInvoicingService.delivery_orders(record)).order_by("pk"):
            ExternalOrderService.invoice_delivery_order(delivery_order, store.issue_invoices, store.email_invoices)

    def due(self) -> Q:
        return Q(Exists(self._uninvoiced(ShopifyInvoicingService.delivery_orders_of_outer_order())))

    @staticmethod
    def _uninvoiced(delivery_orders: QuerySet[DeliveryOrder]) -> QuerySet[DeliveryOrder]:
        return delivery_orders.filter(
            status__in=DELIVERY_ORDER_STOCK_OUT_STATUSES, sales_invoice__isnull=True, replaces__isnull=True
        ).select_related("company", "client")


# ── What is done with refunds of invoiced goods ───────────────────────────────


class RefundStrategy(ABC):
    """How a store settles refunds of goods already invoiced (``ShopifyStore.refund_handling``)."""

    @abstractmethod
    def settle(self, store: ShopifyStore, record: ShopifyOrder, refund: ShopifyRefund) -> None:
        """Settle a pending refund, or leave it pending until it can be."""

    @staticmethod
    def mark(refund: ShopifyRefund, status: RefundStatus, note: str = "") -> None:
        refund.status = status
        refund.note = note
        refund.processed_at = timezone.now()
        refund.save(update_fields=["status", "note", "processed_at", "credit_note"])


class ReviewRefunds(RefundStrategy):
    """Staff settle refunds by hand."""

    def settle(self, store: ShopifyStore, record: ShopifyOrder, refund: ShopifyRefund) -> None:
        self.mark(refund, RefundStatus.REVIEW)


class CreditNoteRefunds(RefundStrategy):
    """A credit note for the refunded products and shipping, priced as they were invoiced."""

    def settle(self, store: ShopifyStore, record: ShopifyOrder, refund: ShopifyRefund) -> None:
        from inventory.models import InventoryItem

        cent = ShopifyService.CENT
        detail = refund.detail
        shipping_net = Decimal(detail.get("shipping", {}).get("net", "0"))
        if not detail.get("lines") and not shipping_net:
            return self.mark(refund, RefundStatus.REVIEW, _("The refund isn't for products or shipping; settle it by hand."))

        invoiced = self._invoiced_left(record)
        items = InventoryItem.objects.in_bulk([line["item"] for line in detail["lines"] if line["item"]])
        lines = []
        for line in detail["lines"]:
            item = items.get(line["item"])
            if item is None:
                return self.mark(
                    refund,
                    RefundStatus.REVIEW,
                    _("The refund includes %(product)s, which is not linked to an ERP item.") % {"product": line["name"]},
                )
            quantity = Decimal(line["quantity"])
            left_quantity, left_net = invoiced.get(item.code, (Decimal("0"), Decimal("0")))
            if quantity > left_quantity:
                return self.mark(refund, RefundStatus.PENDING, _("Waiting until the refunded products are invoiced."))
            net = (left_net * quantity / left_quantity).quantize(cent)
            invoiced[item.code] = (left_quantity - quantity, left_net - net)
            lines.append(CreditNoteLine(item.name, item.code, quantity, net, store.tax_type, Decimal(line["rate"])))

        if shipping_net:
            shipping = store.shipping_item
            if shipping is None:
                return self.mark(
                    refund, RefundStatus.REVIEW, _("The refund includes shipping, but the store has no shipping item.")
                )
            left_net = invoiced.get(shipping.code, (Decimal("0"), Decimal("0")))[1]
            if shipping_net > left_net + cent:
                return self.mark(refund, RefundStatus.PENDING, _("Waiting until the refunded shipping is invoiced."))
            net = left_net if abs(shipping_net - left_net) <= cent else shipping_net
            lines.append(
                CreditNoteLine(shipping.name, shipping.code, Decimal("1"), net, store.tax_type, Decimal(detail["shipping"]["rate"]))
            )

        credited = self._gross(lines)
        if abs(credited - refund.amount) > cent * len(lines):
            return self.mark(
                refund,
                RefundStatus.REVIEW,
                _("The refund of %(refunded)s doesn't match the invoiced products and shipping (%(credited)s); it may include other amounts. Settle it by hand.")
                % {"refunded": refund.amount, "credited": credited},
            )

        invoice = ShopifyInvoicingService.order_invoices(record).select_related("client").first()
        refund.credit_note = ExternalOrderService.credit(
            store.company, invoice.client, lines,
            project_file=record.quote.project_file if record.quote else None,
            issue=store.issue_invoices, send_email=store.email_invoices,
            notes=_("Refund of Shopify order %(order)s") % {"order": record.name},
        )
        self.mark(refund, RefundStatus.CREDITED)

    @staticmethod
    def _invoiced_left(record: ShopifyOrder) -> dict[str, tuple[Decimal, Decimal]]:
        """Per item code, the quantity and net amount invoiced and not credited yet."""
        credit_notes = record.refunds.filter(credit_note__isnull=False).exclude(
            credit_note__status=SalesInvoiceStatus.CANCELLED
        ).values("credit_note")
        rows = (
            SalesInvoiceItem.objects.filter(
                Q(invoice__in=ShopifyInvoicingService.order_invoices(record)) | Q(invoice__in=credit_notes)
            )
            .values("reference")
            .annotate(quantity=Sum("quantity"), net=Sum("line_total"))
        )
        return {row["reference"]: (row["quantity"], row["net"]) for row in rows}

    @staticmethod
    def _gross(lines: list[CreditNoteLine]) -> Decimal:
        """What the credit note adds up to: tax per rate, as an invoice rounds it."""
        bases: dict[Decimal, Decimal] = {}
        for line in lines:
            bases[line.tax_rate] = bases.get(line.tax_rate, Decimal("0")) + line.net_total
        return sum(
            (base + (base * rate / 100).quantize(ShopifyService.CENT) for rate, base in bases.items()), Decimal("0")
        )


# ── The service ───────────────────────────────────────────────────────────────


class ShopifyInvoicingService(ShopifyService):
    # Read with the order (``ShopifyOrderService``); needs only read_orders.
    REFUNDS_FIELDS = """
    refunds(first: 50) {
      id createdAt
      totalRefundedSet { shopMoney { amount } }
      refundLineItems(first: 250) { nodes { quantity lineItem { id name variant { id } taxLines { ratePercentage } } } }
      refundShippingLines(first: 20) { nodes {
        subtotalAmountSet { shopMoney { amount } }
        taxAmountSet { shopMoney { amount } }
      } }
    }
    """
    INVOICING_STRATEGIES: dict[str, InvoicingStrategy] = {
        InvoiceTiming.MANUAL: ManualInvoicing(),
        InvoiceTiming.PAYMENT: InvoiceOnPayment(),
        InvoiceTiming.SHIPMENT: InvoiceEachShipment(),
    }
    REFUND_STRATEGIES: dict[str, RefundStrategy] = {
        RefundHandling.REVIEW: ReviewRefunds(),
        RefundHandling.CREDIT_NOTE: CreditNoteRefunds(),
    }

    @classmethod
    def record_refunds(cls, record: ShopifyOrder, data: dict, settled_on_quote: bool) -> None:
        """Keep the order's new refunds.

        ``settled_on_quote``: nothing is invoiced yet and the quote was just
        rebuilt without the refunded goods, so there is nothing to credit.
        """
        known = set(record.refunds.values_list("refund_id", flat=True))
        for refund in data["refunds"]:
            if refund["id"] in known:
                continue
            amount = cls.money(refund["totalRefundedSet"])
            settled = settled_on_quote or not amount
            ShopifyRefund.objects.create(
                store=record.store,
                order=record,
                refund_id=refund["id"],
                refunded_at=parse_datetime(refund["createdAt"]),
                amount=amount,
                detail=cls._refund_detail(record.store, refund, data),
                status=RefundStatus.NO_CREDIT if settled else RefundStatus.PENDING,
                processed_at=timezone.now() if settled else None,
            )

    @classmethod
    def apply(cls, record: ShopifyOrder) -> None:
        """Create what the order is due: invoices, payments and credit notes.

        The caller holds the order's row lock. A failure is kept on the order
        and retried by ``invoice_pending``; it never undoes the import.
        """
        store = record.store
        if not store.invoices_automatically(record):
            record.refunds.filter(status=RefundStatus.PENDING).update(
                status=RefundStatus.REVIEW, processed_at=timezone.now()
            )
            return
        error = ""
        try:
            with transaction.atomic():
                if record.quote is not None and not record.is_cancelled:
                    cls.INVOICING_STRATEGIES[store.invoice_on].invoice(store, record)
                cls._record_payments(store, record)
                refunds = cls.REFUND_STRATEGIES[store.refund_handling]
                for refund in record.refunds.filter(status=RefundStatus.PENDING).order_by("refunded_at", "pk"):
                    refunds.settle(store, record, refund)
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except ExternalOrderError as exc:
            error = str(exc)
        if record.invoicing_error != error:
            record.invoicing_error = error[:2000]
            record.save(update_fields=["invoicing_error"])

    @classmethod
    def invoice_pending(cls, store: ShopifyStore) -> int:
        """Apply invoicing to the store's orders still due something, e.g. after a failure."""
        if store.invoicing_since is None:
            return 0
        due = Q(invoicing_error__gt="") | Q(refunds__status=RefundStatus.PENDING) | cls.INVOICING_STRATEGIES[store.invoice_on].due()
        if store.record_payments:
            unpaid = SalesInvoice.objects.filter(
                status=SalesInvoiceStatus.FINAL, payment_status=InvoicePaymentStatus.UNPAID, total_amount__gt=0
            )
            due |= Q(financial_status__in=OrderFinancialStatus.captured()) & (
                Q(invoice__in=unpaid) | Exists(cls.delivery_orders_of_outer_order().filter(sales_invoice__in=unpaid))
            )
        order_ids = list(
            store.orders.filter(placed_at__gte=store.invoicing_since).filter(due).values_list("pk", flat=True).distinct()
        )
        for order_id in order_ids:
            cls.apply_locked(order_id)
        return len(order_ids)

    @classmethod
    @transaction.atomic
    def apply_locked(cls, order_id: int) -> None:
        record = (
            ShopifyOrder.objects.select_for_update(of=("self",))
            .select_related("store", "quote", "invoice")
            .get(pk=order_id)
        )
        cls.apply(record)

    @classmethod
    def order_invoices(cls, record: ShopifyOrder) -> QuerySet[SalesInvoice]:
        """The order's invoices that are not cancelled, credit notes aside."""
        return SalesInvoice.objects.filter(
            Q(pk=record.invoice_id) | Q(pk__in=cls.delivery_orders(record).values("sales_invoice"))
        ).exclude(status=SalesInvoiceStatus.CANCELLED)

    @staticmethod
    def delivery_orders(record: ShopifyOrder) -> QuerySet[DeliveryOrder]:
        if record.quote is None:
            return DeliveryOrder.objects.none()
        return ExternalOrderService.delivery_orders(record.quote)

    @staticmethod
    def delivery_orders_of_outer_order() -> QuerySet[DeliveryOrder]:
        """The delivery orders of the order a subquery runs for: its expediente's."""
        return DeliveryOrder.objects.filter(project_file=OuterRef("quote__project_file")).exclude(
            status=DeliveryOrderStatus.CANCELLED
        )

    @classmethod
    def _record_payments(cls, store: ShopifyStore, record: ShopifyOrder) -> None:
        if not store.record_payments or record.financial_status not in OrderFinancialStatus.captured():
            return
        unpaid = cls.order_invoices(record).filter(
            status=SalesInvoiceStatus.FINAL, payment_status=InvoicePaymentStatus.UNPAID, total_amount__gt=0
        )
        for invoice in unpaid:
            ExternalOrderService.record_payment(
                invoice, PaymentMethod.CARD, reference=_("Shopify %(order)s") % {"order": record.name}
            )

    @classmethod
    def _refund_detail(cls, store: ShopifyStore, refund: dict, data: dict) -> dict:
        """What the refund gave back, in ERP terms."""
        item_of_variant = dict(
            store.variant_links.filter(status=VariantLinkStatus.MATCHED).values_list("variant_id", "item_id")
        )
        lines = []
        for node in refund["refundLineItems"]["nodes"]:
            if not node["quantity"]:
                continue
            line_item = node["lineItem"]
            rates = {Decimal(str(tax["ratePercentage"])) for tax in line_item["taxLines"]}
            lines.append({
                "item": item_of_variant.get((line_item["variant"] or {}).get("id")),
                "name": line_item["name"],
                "quantity": str(node["quantity"]),
                "rate": str(max(rates, default=Decimal("0"))),
            })
        net = Decimal("0")
        for node in refund["refundShippingLines"]["nodes"]:
            subtotal, tax = cls.money(node["subtotalAmountSet"]), cls.money(node["taxAmountSet"])
            net += subtotal - tax if data["taxesIncluded"] else subtotal
        shipping_rates = {
            Decimal(str(tax["ratePercentage"]))
            for line in data["shippingLines"]["nodes"]
            for tax in line["taxLines"]
        }
        return {
            "lines": lines,
            "shipping": {"net": str(net), "rate": str(max(shipping_rates, default=Decimal("0")))},
        }
