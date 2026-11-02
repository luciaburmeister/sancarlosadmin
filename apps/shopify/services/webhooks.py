"""
Receiving, processing and subscribing to Shopify webhooks.

https://shopify.dev/docs/apps/build/webhooks
"""

import base64
import hashlib
import hmac
import logging
from collections.abc import Mapping
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from apps.shopify.client import ShopifyClient
from apps.shopify.config import ShopifyApiConfig
from apps.shopify.exceptions import ShopifyAPIError
from apps.shopify.handlers import WebhookHandlers
from apps.shopify.models import ShopifyStore, ShopifyWebhookEvent, WebhookEventStatus
from apps.shopify.services.base import ShopifyService

logger = logging.getLogger(__name__)


class ShopifyWebhookService(ShopifyService):
    # Days a handled delivery is kept. Order deliveries carry customer names and
    # addresses, which Shopify's protected customer data terms only allow keeping
    # as long as needed; what matters is already on the quote and client.
    RETENTION_DAYS = 30
    DELETE_BATCH_SIZE = 5000

    SUBSCRIPTIONS_QUERY = """
    query { webhookSubscriptions(first: 100) { nodes { topic uri } } }
    """
    CREATE_SUBSCRIPTION = """
    mutation ($topic: WebhookSubscriptionTopic!, $uri: String!) {
      webhookSubscriptionCreate(topic: $topic, webhookSubscription: {uri: $uri}) {
        userErrors { field message }
      }
    }
    """

    @staticmethod
    def is_authentic(body: bytes, signature: str, client_secret: str) -> bool:
        """Check X-Shopify-Hmac-SHA256: a base64 HMAC-SHA256 of the raw body."""
        if not signature or not client_secret:
            return False
        digest = hmac.new(client_secret.encode(), body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)

    @staticmethod
    def receive(store: ShopifyStore, headers: Mapping[str, str], payload: dict) -> tuple[ShopifyWebhookEvent, bool]:
        """Save a verified delivery once. Returns (event, created)."""
        return ShopifyWebhookEvent.objects.get_or_create(
            webhook_id=headers["X-Shopify-Webhook-Id"],
            defaults={
                "store": store,
                "topic": headers.get("X-Shopify-Topic", ""),
                "event_id": headers.get("X-Shopify-Event-Id", ""),
                "api_version": headers.get("X-Shopify-API-Version", ""),
                "triggered_at": parse_datetime(headers.get("X-Shopify-Triggered-At", "")),
                "payload": payload,
            },
        )

    @staticmethod
    def enqueue(event_id: int) -> None:
        """Process the event in the background once it is committed.

        If the queue is unreachable the event stays RECEIVED in the inbox,
        where it can be processed again from the admin.
        """
        def queue() -> None:
            from apps.shopify.tasks import process_webhook_event

            try:
                process_webhook_event.delay(event_id)
            except Exception:
                logger.exception("Could not queue Shopify webhook event %s", event_id)

        transaction.on_commit(queue)

    @staticmethod
    def process(event_id: int) -> ShopifyWebhookEvent | None:
        """Run the topic's handler. Processed events are skipped; ignored ones
        are looked at again, so they run once their topic gets a handler."""
        with transaction.atomic():
            event = (
                ShopifyWebhookEvent.objects.select_for_update(of=("self",))
                .select_related("store")
                .filter(pk=event_id)
                .first()
            )
            if event is None or event.status == WebhookEventStatus.PROCESSED:
                return event
            handler = WebhookHandlers.for_topic(event.topic)
            event.attempts += 1
            if handler is None:
                event.status = WebhookEventStatus.IGNORED
                event.error = ""
            else:
                try:
                    with transaction.atomic():
                        event.status = handler.handle(event)
                except Exception as exc:
                    logger.exception("Shopify webhook event %s (%s) failed", event.pk, event.topic)
                    event.status = WebhookEventStatus.FAILED
                    event.error = str(exc)[:2000]
                    handler.on_failure(event, exc)
                else:
                    event.error = ""
            event.processed_at = timezone.now()
            event.save(update_fields=["status", "attempts", "error", "processed_at"])
            return event

    @classmethod
    def delete_old_events(cls, batch_size: int | None = None) -> int:
        """Delete processed and ignored deliveries older than the retention period.

        Received and failed ones stay until they are handled, so nothing is
        lost. Shopify retries a delivery for 48 hours at most, so the webhook
        id is no longer needed to spot a repeat by then.
        """
        batch_size = batch_size or cls.DELETE_BATCH_SIZE
        cutoff = timezone.now() - timedelta(days=cls.RETENTION_DAYS)
        old = ShopifyWebhookEvent.objects.filter(
            status__in=[WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED], received_at__lt=cutoff
        )
        deleted = 0
        # In batches, so a first run over months of deliveries holds no long lock.
        while batch := list(old.values_list("pk", flat=True)[:batch_size]):
            deleted += ShopifyWebhookEvent.objects.filter(pk__in=batch).delete()[0]
        return deleted

    @staticmethod
    def subscription_uri() -> str:
        return ShopifyApiConfig.public_url("shopify_webhook")

    @classmethod
    def sync_subscriptions(cls, store: ShopifyStore) -> list[str]:
        """Subscribe the store to every configured topic it is not yet subscribed to.

        Returns the topics created. Every topic is attempted; if Shopify refuses
        any (e.g. protected customer data not yet granted to the app), the rest
        are still created and ``ShopifyAPIError`` names the refused ones.
        Subscriptions to other addresses are left alone.
        """
        client = ShopifyClient(store)
        uri = cls.subscription_uri()
        existing = {
            node["topic"]
            for node in client.graphql(cls.SUBSCRIPTIONS_QUERY)["webhookSubscriptions"]["nodes"]
            if node.get("uri") == uri
        }
        created, refused = [], []
        for topic in WebhookHandlers.topics():
            enum = topic.upper().replace("/", "_")
            if enum in existing:
                continue
            result = client.graphql(cls.CREATE_SUBSCRIPTION, {"topic": enum, "uri": uri})
            errors = result["webhookSubscriptionCreate"]["userErrors"]
            if errors:
                refused.append(
                    _("Shopify refused the %(topic)s subscription: %(detail)s")
                    % {"topic": topic, "detail": errors[0]["message"]}
                )
            else:
                created.append(topic)
        if refused:
            raise ShopifyAPIError(" ".join(refused))
        return created
