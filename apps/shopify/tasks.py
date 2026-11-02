import logging

from celery import Task, shared_task

logger = logging.getLogger(__name__)


@shared_task(name="apps.shopify.tasks.process_webhook_event")
def process_webhook_event(event_id: int) -> dict:
    """Process one saved Shopify webhook delivery."""
    from apps.shopify.services.webhooks import ShopifyWebhookService

    event = ShopifyWebhookService.process(event_id)
    return {"task": "process_webhook_event", "event_id": event_id, "status": event.status if event else "missing"}


@shared_task(name="apps.shopify.tasks.delete_old_webhook_events")
def delete_old_webhook_events() -> dict:
    """Delete handled webhook deliveries past the retention period.

    Schedule it via a django-celery-beat PeriodicTask (admin > Periodic
    Tasks), e.g. daily at 03:15.
    """
    from apps.shopify.services.webhooks import ShopifyWebhookService

    return {"task": "delete_old_webhook_events", "deleted": ShopifyWebhookService.delete_old_events()}


@shared_task(name="apps.shopify.tasks.push_stock_for_items", bind=True, max_retries=3, default_retry_delay=120)
def push_stock_for_items(self: Task, item_ids: list[int]) -> None:
    """Push these ERP items' stock to every syncing store of their companies."""
    from apps.shopify.exceptions import ShopifyError
    from apps.shopify.models import ShopifyStore
    from apps.shopify.services.stock import ShopifyStockService
    from inventory.models import InventoryItem

    company_ids = InventoryItem.objects.filter(pk__in=item_ids).values_list("company_id", flat=True).distinct()
    failed = False
    for store in ShopifyStore.objects.syncing_stock().filter(company_id__in=company_ids):
        try:
            ShopifyStockService.push(store, item_ids=item_ids)
        except ShopifyError:
            logger.exception("Stock push to Shopify store %s failed", store.pk)
            failed = True
    if failed:
        # Pushes are compare-and-set, so repeating the stores that worked is harmless.
        raise self.retry()


@shared_task(name="apps.shopify.tasks.push_stock_all_stores")
def push_stock_all_stores() -> None:
    """Full stock push for every syncing store: repairs anything a live push missed.

    Schedule it via a django-celery-beat PeriodicTask (admin > Periodic
    Tasks), e.g. nightly at 04:30, after the catalog push.
    """
    from apps.shopify.exceptions import ShopifyError
    from apps.shopify.models import ShopifyStore
    from apps.shopify.services.stock import ShopifyStockService

    for store in ShopifyStore.objects.syncing_stock():
        try:
            ShopifyStockService.push(store)
        except ShopifyError:
            logger.exception("Nightly stock push to Shopify store %s failed", store.pk)


@shared_task(name="apps.shopify.tasks.sync_updated_orders")
def sync_updated_orders() -> None:
    """Import orders updated since each store's last check: catches missed webhooks.

    Also sends shipments and invoices that never went out. Schedule it via a
    django-celery-beat PeriodicTask (admin > Periodic Tasks), e.g. every 10
    minutes.
    """
    from apps.shopify.exceptions import ShopifyError
    from apps.shopify.models import ShopifyStore
    from apps.shopify.services.fulfillments import ShopifyFulfillmentService
    from apps.shopify.services.invoicing import ShopifyInvoicingService
    from apps.shopify.services.orders import ShopifyOrderService

    for store in ShopifyStore.objects.importing_orders():
        try:
            ShopifyOrderService.sync_updated_orders(store)
            ShopifyFulfillmentService.push_pending(store)
        except ShopifyError:
            logger.exception("Checking Shopify store %s for updated orders failed", store.pk)
        ShopifyInvoicingService.invoice_pending(store)


@shared_task(name="apps.shopify.tasks.push_fulfillment", bind=True, max_retries=3, default_retry_delay=120)
def push_fulfillment(self: Task, delivery_order_id: int) -> None:
    """Send a dispatched web order's shipment and tracking to Shopify."""
    from apps.shopify.exceptions import ShopifyError
    from apps.shopify.services.fulfillments import ShopifyFulfillmentService

    try:
        ShopifyFulfillmentService.push_delivery_order(delivery_order_id)
    except ShopifyError as exc:
        raise self.retry(exc=exc)


@shared_task(name="apps.shopify.tasks.invoice_shipment")
def invoice_shipment(delivery_order_id: int) -> None:
    """Invoice a dispatched web order shipment, for stores that invoice on shipment."""
    from apps.shopify.services.fulfillments import ShopifyFulfillmentService
    from apps.shopify.services.invoicing import ShopifyInvoicingService
    from sales.models import DeliveryOrder

    delivery_order = DeliveryOrder.objects.filter(pk=delivery_order_id).first()
    record = ShopifyFulfillmentService.order_of(delivery_order) if delivery_order else None
    if record is not None:
        ShopifyInvoicingService.apply_locked(record.pk)


@shared_task(name="apps.shopify.tasks.push_catalog_for_items", bind=True, max_retries=5, default_retry_delay=60)
def push_catalog_for_items(self: Task, item_ids: list[int]) -> None:
    """Create or update these ERP items' products in every store of their companies."""
    from apps.shopify.models import ShopifyStore
    from apps.shopify.services.catalog import CatalogPushBusy, ShopifyCatalogService
    from inventory.models import InventoryItem

    company_ids = InventoryItem.objects.filter(pk__in=item_ids).values_list("company_id", flat=True).distinct()
    busy = False
    for store in ShopifyStore.objects.pushing_catalog().filter(company_id__in=company_ids):
        try:
            result = ShopifyCatalogService.push_catalog(store, item_ids=item_ids)
        except CatalogPushBusy:
            busy = True
            continue
        for error in result.errors:
            logger.warning("Shopify catalog push to store %s: %s", store.pk, error)
    if busy:
        raise self.retry()


@shared_task(name="apps.shopify.tasks.push_catalog_for_store", bind=True, max_retries=15, default_retry_delay=60)
def push_catalog_for_store(self: Task, store_id: int) -> None:
    """Full catalog push of one store, started from its page."""
    from apps.shopify.exceptions import ShopifyError
    from apps.shopify.models import ShopifyStore
    from apps.shopify.services.catalog import CatalogPushBusy, ShopifyCatalogService

    store = ShopifyStore.objects.filter(pk=store_id).first()
    if store is None:
        return
    try:
        ShopifyCatalogService.push_whole_catalog(store)
    except CatalogPushBusy:
        raise self.retry()  # e.g. a live push of a few items; this one follows
    except ShopifyError:
        logger.exception("Catalog push to Shopify store %s failed", store.pk)


@shared_task(name="apps.shopify.tasks.push_catalog_all_stores")
def push_catalog_all_stores() -> None:
    """Full catalog push for every store: repairs anything a live push missed.

    Schedule it via a django-celery-beat PeriodicTask (admin > Periodic
    Tasks), e.g. nightly at 04:00, before the stock push.
    """
    from apps.shopify.exceptions import ShopifyError
    from apps.shopify.models import ShopifyStore
    from apps.shopify.services.catalog import ShopifyCatalogService

    for store in ShopifyStore.objects.pushing_catalog():
        try:
            result = ShopifyCatalogService.push_whole_catalog(store)
        except ShopifyError:
            logger.exception("Nightly catalog push to Shopify store %s failed", store.pk)
            continue
        for error in result.errors:
            logger.warning("Shopify catalog push to store %s: %s", store.pk, error)
