"""
Keeping a store's shipments in step between the ERP and Shopify.

Each store chooses who ships (``ShopifyStore.fulfillment_by``):

* ERP warehouse: a paid order gets a delivery order pending shipment. Once it
  is dispatched, a Shopify fulfillment with its tracking is created for the
  same lines, and Shopify emails the customer if the store wants it.
* Shopify or a partner: every successful fulfillment in Shopify becomes a
  dispatched delivery order in the ERP for its lines, which takes the stock
  out of the warehouse.

``ShopifyFulfillment`` links each shipment to its delivery order, so neither
side is ever created twice.

A shipment later cancelled in Shopify is flagged for review, never undone by
itself: staff record whether the goods are back in the warehouse (the
delivery order is cancelled and the stock returns) or were lost on the way
(the stock stays out and the goods ship again on a replacement delivery
order, which is not invoiced).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.shopify.client import ShopifyClient
from apps.shopify.enums import FulfillmentOrderStatus, FulfillmentStatus
from apps.shopify.exceptions import ShopifyError
from apps.shopify.models import (
    FulfillmentBy,
    FulfillmentCancellation,
    ShopifyFulfillment,
    ShopifyOrder,
    ShopifyStore,
    VariantLinkStatus,
)
from apps.shopify.services.base import ShopifyService
from invoices.models import SalesInvoiceStatus
from invoices.services.delivery_order_service import DeliveryOrderService
from sales.models import (
    DELIVERY_ORDER_STOCK_OUT_STATUSES,
    DeliveryOrder,
    DeliveryOrderStatus,
    DeliveryShippingMethod,
    QuoteInventoryItem,
    QuoteStatus,
    ShippingAgency,
)
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService
from sales.services.external_order import ExternalOrderService

class ShopifyFulfillmentError(ShopifyError):
    """A shipment can't be matched between the ERP and Shopify; the message says why."""


class ShopifyFulfillmentService(ShopifyService):
    # Read with the order (``ShopifyOrderService``); needs only read_orders.
    FULFILLMENTS_FIELDS = """
    fulfillments(first: 50) {
      id status
      trackingInfo(first: 1) { company number url }
      fulfillmentLineItems(first: 250) { nodes { quantity lineItem { variant { id } } } }
    }
    """
    FULFILLMENT_ORDERS_QUERY = """
    query ($id: ID!) {
      order(id: $id) {
        fulfillmentOrders(first: 20) { nodes {
          id status
          lineItems(first: 250) { nodes { id remainingQuantity lineItem { variant { id } } } }
        } }
      }
    }
    """
    FULFILLMENT_CREATE = """
    mutation ($fulfillment: FulfillmentInput!) {
      fulfillmentCreate(fulfillment: $fulfillment) {
        fulfillment { id }
        userErrors { field message }
      }
    }
    """

    # ── ERP warehouse ships ──────────────────────────────────────────────

    @staticmethod
    def create_pending_delivery(record: ShopifyOrder) -> DeliveryOrder | None:
        """Give a paid order a delivery order pending shipment, unless it has one."""
        if ExternalOrderService.delivery_orders(record.quote).exists():
            return None
        return DeliveryOrderService.deliver_remaining(
            record.quote.project_file, external_id=record.order_id, delivery_address=record.shipping_address
        )

    @classmethod
    def push_delivery_order(cls, delivery_order_id: int) -> ShopifyFulfillment | None:
        """Create the Shopify fulfillment of a dispatched web order delivery order.

        ``None`` when there is nothing to push (not dispatched, not a web
        order, or the store doesn't ship from the ERP). Errors are kept on the
        ``ShopifyFulfillment`` and raised, so the caller can retry.
        """
        delivery_order = DeliveryOrder.objects.select_related("shipping_agency").filter(pk=delivery_order_id).first()
        if delivery_order is None or delivery_order.status not in DELIVERY_ORDER_STOCK_OUT_STATUSES:
            return None
        record = cls.order_of(delivery_order)
        if record is None:
            return None
        store = record.store
        if not (store.is_active and store.is_connected and store.import_orders and store.fulfillment_by == FulfillmentBy.ERP):
            return None

        error = None
        with transaction.atomic():
            fulfillment, _created = ShopifyFulfillment.objects.get_or_create(
                store=store, delivery_order=delivery_order, defaults={"order": record}
            )
            # One push per delivery order at a time: a second waits, then sees the id.
            fulfillment = ShopifyFulfillment.objects.select_for_update().get(pk=fulfillment.pk)
            if fulfillment.fulfillment_id:
                return fulfillment
            try:
                fulfillment.fulfillment_id = cls._create_in_shopify(store, record, delivery_order) or ""
                fulfillment.error = ""
            except ShopifyError as exc:
                fulfillment.error = str(exc)[:2000]
                error = exc
            fulfillment.synced_at = timezone.now()
            fulfillment.save()
        if error is not None:
            raise error
        return fulfillment

    @classmethod
    def push_pending(cls, store: ShopifyStore, orders: QuerySet[ShopifyOrder] | None = None) -> tuple[int, int]:
        """Push the dispatched delivery orders of the store's orders not yet in Shopify.

        Catches pushes that never ran, e.g. while the task queue was down.
        ``orders`` limits it to those ``ShopifyOrder`` records. Returns
        ``(pushed, failed)``; failures stay recorded on their fulfillment.
        """
        if store.fulfillment_by != FulfillmentBy.ERP:
            return 0, 0
        orders = store.orders.all() if orders is None else orders
        pending = (
            DeliveryOrder.objects.filter(
                company_id=store.company_id,
                status__in=DELIVERY_ORDER_STOCK_OUT_STATUSES,
                project_file__in=orders.values("quote__project_file"),
            )
            .exclude(shopify_fulfillment__fulfillment_id__gt="")
            .values_list("pk", flat=True)
        )
        pushed = failed = 0
        for delivery_order_id in pending:
            try:
                if cls.push_delivery_order(delivery_order_id):
                    pushed += 1
            except ShopifyError:
                failed += 1
        return pushed, failed

    @staticmethod
    def order_of(delivery_order: DeliveryOrder) -> ShopifyOrder | None:
        """The web order a delivery order ships, replacements included: the one of its expediente."""
        if delivery_order.project_file_id is None:
            return None
        return (
            ShopifyOrder.objects.select_related("store")
            .filter(store__company_id=delivery_order.company_id, quote__project_file_id=delivery_order.project_file_id)
            .first()
        )

    @classmethod
    def _create_in_shopify(cls, store: ShopifyStore, record: ShopifyOrder, delivery_order: DeliveryOrder) -> str | None:
        """Fulfill the delivery order's products in Shopify; ``None`` if it ships none."""
        variant_of = dict(
            store.variant_links.filter(status=VariantLinkStatus.MATCHED).values_list("item_id", "variant_id")
        )
        wanted = Counter()
        for line in delivery_order.items.top_level():
            if line.inventory_item_id in variant_of and line.quantity > 0:
                wanted[variant_of[line.inventory_item_id]] += int(line.quantity)
        if not wanted:
            return None  # services only, e.g. shipping costs: nothing to ship in Shopify

        client = ShopifyClient(store)
        order = client.graphql(cls.FULFILLMENT_ORDERS_QUERY, {"id": record.order_id})["order"]
        if order is None:
            raise ShopifyFulfillmentError(_("The order no longer exists in Shopify."))
        by_fulfillment_order = defaultdict(list)
        for fulfillment_order in order["fulfillmentOrders"]["nodes"]:
            if fulfillment_order["status"] not in FulfillmentOrderStatus.open():
                continue
            for line in fulfillment_order["lineItems"]["nodes"]:
                variant = (line["lineItem"]["variant"] or {}).get("id")
                take = min(wanted[variant], line["remainingQuantity"])
                if take > 0:
                    by_fulfillment_order[fulfillment_order["id"]].append({"id": line["id"], "quantity": take})
                    wanted[variant] -= take
        if +wanted:
            raise ShopifyFulfillmentError(
                _("Shopify has less left to ship than delivery order %(number)s; check whether it was already fulfilled in Shopify.")
                % {"number": delivery_order.number}
            )

        fulfillment = {
            "lineItemsByFulfillmentOrder": [
                {"fulfillmentOrderId": fulfillment_order_id, "fulfillmentOrderLineItems": lines}
                for fulfillment_order_id, lines in by_fulfillment_order.items()
            ],
            "notifyCustomer": store.notify_customer,
        }
        tracking = {
            "company": delivery_order.shipping_agency.name if delivery_order.shipping_agency else "",
            "number": delivery_order.shipment_number,
            "url": delivery_order.tracking_link,
        }
        if any(tracking.values()):
            fulfillment["trackingInfo"] = {key: value for key, value in tracking.items() if value}
        result = client.graphql(cls.FULFILLMENT_CREATE, {"fulfillment": fulfillment})["fulfillmentCreate"]
        cls.raise_user_errors(result["userErrors"], _("Shopify refused the fulfillment: %(detail)s"))
        return result["fulfillment"]["id"]

    # ── Shipments cancelled in Shopify ───────────────────────────────────

    @staticmethod
    def note_cancellations(record: ShopifyOrder, fulfillments: list[dict]) -> None:
        """Flag the order's shipments cancelled in Shopify for staff to review."""
        cancelled = [f["id"] for f in fulfillments if f["status"] == FulfillmentStatus.CANCELLED]
        if not cancelled:
            return
        for fulfillment in record.fulfillments.filter(fulfillment_id__in=cancelled, cancellation="").select_related("delivery_order"):
            delivery_order = fulfillment.delivery_order
            shipped = delivery_order is not None and delivery_order.status in DELIVERY_ORDER_STOCK_OUT_STATUSES
            # Already taken back in the ERP: nothing left to decide.
            fulfillment.cancellation = FulfillmentCancellation.REVIEW if shipped else FulfillmentCancellation.RETURNED
            fulfillment.cancelled_at = timezone.now()
            fulfillment.save(update_fields=["cancellation", "cancelled_at"])

    @classmethod
    @transaction.atomic
    def goods_returned(cls, fulfillment: ShopifyFulfillment) -> None:
        """The cancelled shipment's goods are back: cancel its delivery order, returning the stock."""
        fulfillment, record = cls._lock_for_review(fulfillment)
        delivery_order = DeliveryOrder.objects.select_for_update(of=("self",)).select_related("sales_invoice").get(
            pk=fulfillment.delivery_order_id
        )
        invoice = delivery_order.sales_invoice
        if invoice is not None and invoice.status != SalesInvoiceStatus.CANCELLED and invoice.pk != record.invoice_id:
            raise ValidationError(
                _("Delivery order %(number)s is invoiced on %(invoice)s: cancel that invoice or issue a credit note first.")
                % {"number": delivery_order.number, "invoice": invoice.display_number}
            )
        if delivery_order.status == DeliveryOrderStatus.DELIVERED:
            DeliveryOrderFulfillmentService.undo(delivery_order)
        DeliveryOrderFulfillmentService.cancel(delivery_order)
        if invoice is not None:
            # The order's invoice bills the shipment that replaces this one instead.
            delivery_order.sales_invoice = None
            delivery_order.save(update_fields=["sales_invoice"])
        fulfillment.cancellation = FulfillmentCancellation.RETURNED
        fulfillment.save(update_fields=["cancellation"])
        if record.store.fulfillment_by == FulfillmentBy.ERP and cls._still_sold(record):
            pending = DeliveryOrderService.deliver_remaining(
                record.quote.project_file, external_id=record.order_id, delivery_address=record.shipping_address
            )
            if pending is not None and record.invoice_id and record.invoice.status != SalesInvoiceStatus.CANCELLED:
                ExternalOrderService.bill_delivery_orders(record.quote, record.invoice)

    @classmethod
    @transaction.atomic
    def goods_lost(cls, fulfillment: ShopifyFulfillment) -> None:
        """The cancelled shipment's goods were lost: the stock stays out and they ship again.

        Where the ERP ships, a replacement delivery order is created at once;
        where Shopify ships, it is created with Shopify's next shipment.
        """
        fulfillment, record = cls._lock_for_review(fulfillment)
        fulfillment.cancellation = FulfillmentCancellation.LOST
        fulfillment.save(update_fields=["cancellation"])
        if record.store.fulfillment_by == FulfillmentBy.ERP and cls._still_sold(record):
            DeliveryOrderService.create_replacement(fulfillment.delivery_order)

    @staticmethod
    def _lock_for_review(fulfillment: ShopifyFulfillment) -> tuple[ShopifyFulfillment, ShopifyOrder]:
        fulfillment = ShopifyFulfillment.objects.select_for_update(of=("self",)).select_related(
            "order__store", "order__quote", "order__invoice", "delivery_order"
        ).get(pk=fulfillment.pk)
        if fulfillment.cancellation != FulfillmentCancellation.REVIEW or fulfillment.delivery_order is None:
            raise ValidationError(_("This shipment has no cancellation to review."))
        return fulfillment, fulfillment.order

    @staticmethod
    def _still_sold(record: ShopifyOrder) -> bool:
        return record.quote is not None and record.quote.status == QuoteStatus.ACCEPTED and not record.is_cancelled

    # ── Shopify or a partner ships ───────────────────────────────────────

    @classmethod
    def follow_shopify_fulfillments(cls, store: ShopifyStore, record: ShopifyOrder, fulfillments: list[dict]) -> None:
        """Turn each new successful Shopify fulfillment into a dispatched delivery order."""
        known = set(record.fulfillments.exclude(fulfillment_id="").values_list("fulfillment_id", flat=True))
        new = [f for f in fulfillments if f["status"] == FulfillmentStatus.SUCCESS and f["id"] not in known]
        if not new:
            return
        lines = list(record.quote.inventory_items.all())
        line_of_item = {line.inventory_item_id: line for line in lines}
        item_of_variant = dict(
            store.variant_links.filter(status=VariantLinkStatus.MATCHED).values_list("variant_id", "item_id")
        )
        for fulfillment in new:
            items = Counter()
            for node in fulfillment["fulfillmentLineItems"]["nodes"]:
                variant = ((node["lineItem"] or {}).get("variant") or {}).get("id")
                line = line_of_item.get(item_of_variant.get(variant))
                if line is None:
                    raise ShopifyFulfillmentError(
                        _("A shipment contains a product that is not on quote %(quote)s.") % {"quote": record.quote}
                    )
                items[line.inventory_item_id] += node["quantity"]
            try:
                delivery_order = cls._replacement_for(record, items)
                if delivery_order is None:
                    line_ct = ContentType.objects.get_for_model(QuoteInventoryItem).pk
                    quantities = {(line_ct, line_of_item[item_id].pk): quantity for item_id, quantity in items.items()}
                    # Shipping costs aren't shipped products: they go with the first
                    # shipment (once delivered, they are no longer deliverable).
                    for line in lines:
                        if line.inventory_item_id == store.shipping_item_id:
                            quantities[(line_ct, line.pk)] = line.quantity
                    delivery_order = DeliveryOrderService.deliver_remaining(
                        record.quote.project_file, quantities=quantities, external_id=record.order_id,
                        delivery_address=record.shipping_address,
                    )
            except ValidationError as exc:
                raise ShopifyFulfillmentError(" ".join(exc.messages)) from exc
            if delivery_order is not None:
                tracking = (fulfillment["trackingInfo"] or [{}])[0]
                DeliveryOrderFulfillmentService.dispatch(
                    delivery_order,
                    shipping_method=DeliveryShippingMethod.CARRIER,
                    shipping_agency=cls._agency(store, tracking.get("company")),
                    shipment_number=tracking.get("number") or "",
                    tracking_url=tracking.get("url") or "",
                )
            ShopifyFulfillment.objects.create(
                store=store,
                order=record,
                fulfillment_id=fulfillment["id"],
                delivery_order=delivery_order,
                synced_at=timezone.now(),
            )

    @staticmethod
    def _replacement_for(record: ShopifyOrder, items: Counter) -> DeliveryOrder | None:
        """A replacement delivery order when the shipment re-sends goods recorded as lost."""
        lost = record.fulfillments.filter(cancellation=FulfillmentCancellation.LOST).select_related("delivery_order")
        for fulfillment in lost:
            left = DeliveryOrderService.replaceable_quantities(fulfillment.delivery_order)
            if all(quantity <= left.get(item_id, 0) for item_id, quantity in items.items()):
                return DeliveryOrderService.create_replacement(fulfillment.delivery_order, quantities=items)
        return None

    @staticmethod
    def _agency(store: ShopifyStore, carrier: str | None) -> ShippingAgency | None:
        if not carrier:
            return None
        return ShippingAgency.objects.filter(company_id=store.company_id, name__iexact=carrier, is_active=True).first()
