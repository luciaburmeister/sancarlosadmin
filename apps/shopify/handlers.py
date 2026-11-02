"""
What the ERP does with each webhook topic.

A handler receives a saved ``ShopifyWebhookEvent`` and raises to mark it
failed, or returns ``WebhookEventStatus.IGNORED`` when the event doesn't apply
(e.g. the store doesn't import orders). Topics without a handler are recorded
as ignored, so a topic subscribed before its handler exists loses nothing: the
events can be processed again once it does.
"""

from abc import ABC, abstractmethod

from apps.shopify.models import ShopifyWebhookEvent, WebhookEventStatus


class WebhookHandler(ABC):
    @abstractmethod
    def handle(self, event: ShopifyWebhookEvent) -> WebhookEventStatus:
        """Apply the event; return PROCESSED, or IGNORED when it doesn't apply."""

    def on_failure(self, event: ShopifyWebhookEvent, error: Exception) -> None:
        """Run after ``handle`` raised and its changes were rolled back, so what it records survives."""

    @staticmethod
    def resource_id(event: ShopifyWebhookEvent) -> str:
        resource_id = event.payload.get("admin_graphql_api_id")
        if not resource_id:
            raise ValueError(f"The {event.topic} payload has no admin_graphql_api_id.")
        return resource_id


class AppUninstalledHandler(WebhookHandler):
    def handle(self, event: ShopifyWebhookEvent) -> WebhookEventStatus:
        """The store removed the app: its tokens no longer work."""
        event.store.disconnect()
        return WebhookEventStatus.PROCESSED


class ProductUpdateHandler(WebhookHandler):
    def handle(self, event: ShopifyWebhookEvent) -> WebhookEventStatus:
        """Re-read the product from Shopify and re-link its variants to ERP items."""
        from apps.shopify.services.catalog import ShopifyCatalogService

        ShopifyCatalogService.refresh_product(event.store, self.resource_id(event))
        return WebhookEventStatus.PROCESSED


class OrderHandler(WebhookHandler):
    def handle(self, event: ShopifyWebhookEvent) -> WebhookEventStatus:
        """Create or update the order's ERP quote from Shopify's current order."""
        from apps.shopify.services.orders import ShopifyOrderService

        if not event.store.import_orders:
            return WebhookEventStatus.IGNORED
        record = ShopifyOrderService.sync_order(event.store, self.resource_id(event))
        return WebhookEventStatus.IGNORED if record is None else WebhookEventStatus.PROCESSED

    def on_failure(self, event: ShopifyWebhookEvent, error: Exception) -> None:
        """Show why the order couldn't be imported in the Shopify orders list."""
        from apps.shopify.services.orders import ShopifyOrderService

        order_id = event.payload.get("admin_graphql_api_id")
        if order_id:
            ShopifyOrderService.record_failure(event.store, order_id, error)


class WebhookHandlers:
    """The handler of each topic; every topic here is subscribed for each store."""

    REGISTRY: dict[str, WebhookHandler] = {
        "app/uninstalled": AppUninstalledHandler(),
        "products/update": ProductUpdateHandler(),
        "orders/create": OrderHandler(),
        "orders/updated": OrderHandler(),
        "orders/cancelled": OrderHandler(),
    }

    @classmethod
    def for_topic(cls, topic: str) -> WebhookHandler | None:
        return cls.REGISTRY.get(topic)

    @classmethod
    def topics(cls) -> tuple[str, ...]:
        return tuple(cls.REGISTRY)
