"""
Pushing ERP changes to Shopify (stock, products and shipments) and invoicing
shipped web orders.

The ERP calls this explicitly where the change is written: ``ItemTransaction.save``
and ``refresh_stock_position`` (stock), ``InventoryItem.save`` (products),
``DeliveryOrder.save`` (shipments) and ``ShopifyStore.save`` (cached settings).
Every item touched inside one database transaction is collected and pushed by
a single task once the transaction commits, so a rolled-back change never
reaches Shopify and a 20-line delivery costs one push, not twenty. Nothing
here calls Shopify inside the ERP's own transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from django.core.cache import cache
from django.db import transaction
from django.db.models import QuerySet

from apps.shopify.models import FulfillmentBy, InvoiceTiming, ShopifyFulfillment, ShopifyStore, ShopifyVariantLink
from inventory.models import InventoryItem
from sales.models import DELIVERY_ORDER_STOCK_OUT_STATUSES, DeliveryOrder

logger = logging.getLogger(__name__)


class _PendingPush:
    """The item ids one transaction touched, pushed by one task after commit."""

    def __init__(self, enqueue: Callable[[set[int]], None]) -> None:
        self.enqueue = enqueue
        self.item_ids: set[int] = set()

    def flush(self) -> None:
        item_ids, self.item_ids = self.item_ids, set()
        self.enqueue(item_ids)


class ShopifyPushScheduler:
    """Queues what an ERP change must send to Shopify, once the change commits.

    Each "does any store use this?" answer is cached briefly, so saves in
    companies without a Shopify store cost nothing.
    """

    CACHE_SECONDS = 60
    STOCK_SYNC_KEY = "shopify:stock-sync-enabled"
    CATALOG_PUSH_KEY = "shopify:catalog-push-enabled"
    ERP_SHIPS_KEY = "shopify:erp-ships"
    INVOICES_SHIPMENTS_KEY = "shopify:invoices-shipments"

    # ── Items ─────────────────────────────────────────────────────────────

    @classmethod
    def schedule_stock_push(cls, item_ids: Iterable[int | None]) -> None:
        """Push these items' stock to Shopify after the current transaction commits."""
        cls._schedule("stock_push", cls.enqueue_stock_push, item_ids)

    @classmethod
    def schedule_catalog_push(cls, item_ids: Iterable[int | None]) -> None:
        """Create or update these items' Shopify products after the current transaction commits."""
        cls._schedule("catalog_push", cls.enqueue_catalog_push, item_ids)

    @classmethod
    def enqueue_stock_push(cls, item_ids: set[int]) -> None:
        if not item_ids or not cls._used(cls.STOCK_SYNC_KEY, ShopifyStore.objects.syncing_stock()):
            return
        from apps.shopify.tasks import push_stock_for_items

        try:
            push_stock_for_items.delay(sorted(item_ids))
        except Exception:
            # Never let a queue outage break the ERP; the periodic full push catches up.
            logger.exception("Could not queue the Shopify stock push for %s items", len(item_ids))

    @classmethod
    def enqueue_catalog_push(cls, item_ids: set[int]) -> None:
        if not item_ids or not cls._used(cls.CATALOG_PUSH_KEY, ShopifyStore.objects.pushing_catalog()):
            return
        from apps.shopify.tasks import push_catalog_for_items

        try:
            push_catalog_for_items.delay(sorted(item_ids))
        except Exception:
            # The periodic full catalog push catches up.
            logger.exception("Could not queue the Shopify catalog push for %s items", len(item_ids))

    @classmethod
    def item_saved(cls, item: InventoryItem) -> None:
        # An item no longer sold online is pushed too, so its product is archived.
        if item.sell_online or (
            cls._used(cls.CATALOG_PUSH_KEY, ShopifyStore.objects.pushing_catalog()) and cls._has_live_product(item)
        ):
            cls.schedule_catalog_push([item.pk])

    # ── Delivery orders ──────────────────────────────────────────────────

    @classmethod
    def delivery_order_saved(cls, delivery_order: DeliveryOrder) -> None:
        """A dispatched web order shipment goes to Shopify and, where the store wants it, is invoiced."""
        if not delivery_order.external_id or delivery_order.status not in DELIVERY_ORDER_STOCK_OUT_STATUSES:
            return
        delivery_order_id = delivery_order.pk
        if cls._erp_ships() and not (
            ShopifyFulfillment.objects.filter(delivery_order_id=delivery_order_id).exclude(fulfillment_id="").exists()
        ):
            transaction.on_commit(lambda: cls.enqueue_fulfillment_push(delivery_order_id))
        # A replacement re-sends lost goods: the original shipment carries the sale.
        if not delivery_order.sales_invoice_id and not delivery_order.replaces_id and cls._invoices_shipments():
            transaction.on_commit(lambda: cls.enqueue_shipment_invoice(delivery_order_id))

    @staticmethod
    def enqueue_fulfillment_push(delivery_order_id: int) -> None:
        from apps.shopify.tasks import push_fulfillment

        try:
            push_fulfillment.delay(delivery_order_id)
        except Exception:
            # The periodic order check pushes it later (ShopifyFulfillmentService.push_pending).
            logger.exception("Could not queue the Shopify fulfillment for delivery order %s", delivery_order_id)

    @staticmethod
    def enqueue_shipment_invoice(delivery_order_id: int) -> None:
        from apps.shopify.tasks import invoice_shipment

        try:
            invoice_shipment.delay(delivery_order_id)
        except Exception:
            # The periodic order check invoices it later (ShopifyInvoicingService.invoice_pending).
            logger.exception("Could not queue the invoice of delivery order %s", delivery_order_id)

    # ── Store settings ───────────────────────────────────────────────────

    @classmethod
    def forget_store_settings(cls) -> None:
        """A store changed: the cached "does any store use this?" answers may be wrong now."""
        cache.delete_many([cls.STOCK_SYNC_KEY, cls.CATALOG_PUSH_KEY, cls.ERP_SHIPS_KEY, cls.INVOICES_SHIPMENTS_KEY])

    @classmethod
    def _erp_ships(cls) -> bool:
        return cls._used(cls.ERP_SHIPS_KEY, ShopifyStore.objects.importing_orders().filter(fulfillment_by=FulfillmentBy.ERP))

    @classmethod
    def _invoices_shipments(cls) -> bool:
        return cls._used(
            cls.INVOICES_SHIPMENTS_KEY, ShopifyStore.objects.importing_orders().filter(invoice_on=InvoiceTiming.SHIPMENT)
        )

    @classmethod
    def _used(cls, key: str, stores: QuerySet[ShopifyStore]) -> bool:
        return cache.get_or_set(key, stores.exists, cls.CACHE_SECONDS)

    @staticmethod
    def _has_live_product(item: InventoryItem) -> bool:
        return (
            ShopifyVariantLink.objects.filter(item=item, archived_from="", store__push_catalog=True)
            .exclude(pushed_signature="")
            .exists()
        )

    @staticmethod
    def _schedule(name: str, enqueue: Callable[[set[int]], None], item_ids: Iterable[int | None]) -> None:
        item_ids = {item_id for item_id in item_ids if item_id}
        if not item_ids:
            return
        connection = transaction.get_connection()
        attribute = f"_shopify_pending_{name}"
        pending = getattr(connection, attribute, None)
        # A rolled-back block discards its on-commit callbacks; a pending push
        # whose flush is no longer registered belongs to such a block.
        if pending is None or not any(entry[1] == pending.flush for entry in connection.run_on_commit):
            pending = _PendingPush(enqueue)
            setattr(connection, attribute, pending)
            pending.item_ids |= item_ids
            transaction.on_commit(pending.flush)  # runs at once outside a transaction
        else:
            pending.item_ids |= item_ids
