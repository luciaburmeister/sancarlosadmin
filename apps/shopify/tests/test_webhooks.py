import base64
import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api.fleet.tests.helpers import make_company, make_user
from apps.shopify.handlers import WebhookHandlers
from apps.shopify.admin import ShopifyWebhookEventAdmin
from apps.shopify.exceptions import ShopifyAPIError
from apps.shopify.models import ShopifyApp, ShopifyStore, ShopifyWebhookEvent, WebhookEventStatus
from apps.shopify.services.webhooks import ShopifyWebhookService
from company.models import CompanyUser

SECRET = "app-client-secret"


def sign(body: bytes, secret=SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class ShopifyWebhookViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Company A")
        cls.app = ShopifyApp(name="Pilot app", client_id="client-123")
        cls.app.set_client_secret(SECRET)
        cls.app.save()

    def setUp(self):
        cache.clear()  # the rejection throttle counts in the cache
        self.store = ShopifyStore(company=self.company, app=self.app, name="A", shop_domain="a.myshopify.com")
        self.store.set_tokens(access_token="shpat", access_token_expires_at=None, refresh_token="", refresh_token_expires_at=None)
        self.store.save()

    def deliver(self, payload, topic="orders/create", webhook_id="wh-1", shop="a.myshopify.com", signature=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("shopify_webhook"),
                data=body,
                content_type="application/json",
                HTTP_X_SHOPIFY_SHOP_DOMAIN=shop,
                HTTP_X_SHOPIFY_TOPIC=topic,
                HTTP_X_SHOPIFY_WEBHOOK_ID=webhook_id,
                HTTP_X_SHOPIFY_HMAC_SHA256=signature if signature is not None else sign(body),
            )

    def test_verified_delivery_is_saved_and_processed(self):
        response = self.deliver({"id": 1001})
        self.assertEqual(response.status_code, 200)
        event = ShopifyWebhookEvent.objects.get()
        self.assertEqual((event.store, event.topic, event.payload), (self.store, "orders/create", {"id": 1001}))
        # The store doesn't import orders: kept in the inbox as ignored.
        self.assertEqual(event.status, WebhookEventStatus.IGNORED)

    def test_app_uninstalled_disconnects_the_store(self):
        self.deliver({"id": 1}, topic="app/uninstalled")
        self.store.refresh_from_db()
        self.assertFalse(self.store.is_connected)
        self.assertEqual(ShopifyWebhookEvent.objects.get().status, WebhookEventStatus.PROCESSED)

    def test_repeated_delivery_is_saved_once(self):
        self.deliver({"id": 1001})
        response = self.deliver({"id": 1001})
        self.assertEqual(response.json(), {"status": "duplicate"})
        self.assertEqual(ShopifyWebhookEvent.objects.count(), 1)

    def test_unverified_deliveries_are_rejected(self):
        body = json.dumps({"id": 1}).encode()
        cases = {
            "forged signature": {"signature": sign(body, "guessed-secret")},
            "missing signature": {"signature": ""},
            "unknown shop": {"shop": "unknown.myshopify.com"},
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                self.assertEqual(self.deliver(body, **kwargs).status_code, 401)
        self.assertFalse(ShopifyWebhookEvent.objects.exists())

    def test_signature_from_another_stores_app_is_rejected(self):
        other_app = ShopifyApp(name="Other app", client_id="client-456")
        other_app.set_client_secret("other-secret")
        other_app.save()
        ShopifyStore.objects.create(company=make_company(name="Company B"), app=other_app, name="B", shop_domain="b.myshopify.com")
        body = json.dumps({"id": 1}).encode()
        # Signed with A's secret but claiming to be B: must not land on B.
        self.assertEqual(self.deliver(body, shop="b.myshopify.com", signature=sign(body)).status_code, 401)
        self.assertFalse(ShopifyWebhookEvent.objects.exists())

    def test_inactive_store_is_acknowledged_but_not_saved(self):
        self.store.is_active = False
        self.store.save()
        self.assertEqual(self.deliver({"id": 1}).json(), {"status": "ignored"})
        self.assertFalse(ShopifyWebhookEvent.objects.exists())

    def test_invalid_json_is_rejected(self):
        self.assertEqual(self.deliver(b"not json").status_code, 400)


class ShopifyWebhookProcessingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = ShopifyStore.objects.create(company=make_company(name="Company A"), name="A", shop_domain="a.myshopify.com")

    def event(self, topic):
        return ShopifyWebhookEvent.objects.create(store=self.store, webhook_id=f"wh-{topic}", topic=topic, payload={})

    @staticmethod
    def handler(**handle):
        return mock.Mock(handle=mock.Mock(**({"return_value": WebhookEventStatus.PROCESSED} | handle)))

    def test_failing_handler_is_recorded_and_can_succeed_later(self):
        event = self.event("carts/update")
        broken = self.handler(side_effect=ValueError("no matching item"))
        with mock.patch.dict(WebhookHandlers.REGISTRY, {"carts/update": broken}):
            event = ShopifyWebhookService.process(event.pk)
        self.assertEqual((event.status, event.error, event.attempts), (WebhookEventStatus.FAILED, "no matching item", 1))
        broken.on_failure.assert_called_once()

        with mock.patch.dict(WebhookHandlers.REGISTRY, {"carts/update": self.handler()}):
            event = ShopifyWebhookService.process(event.pk)
        self.assertEqual((event.status, event.error, event.attempts), (WebhookEventStatus.PROCESSED, "", 2))

    def test_ignored_event_runs_once_its_topic_has_a_handler(self):
        event = ShopifyWebhookService.process(self.event("carts/update").pk)
        self.assertEqual(event.status, WebhookEventStatus.IGNORED)
        handler = self.handler()
        with mock.patch.dict(WebhookHandlers.REGISTRY, {"carts/update": handler}):
            event = ShopifyWebhookService.process(event.pk)
        handler.handle.assert_called_once()
        self.assertEqual(event.status, WebhookEventStatus.PROCESSED)

    def test_processed_event_is_not_run_twice(self):
        handler = self.handler()
        event = self.event("carts/update")
        with mock.patch.dict(WebhookHandlers.REGISTRY, {"carts/update": handler}):
            ShopifyWebhookService.process(event.pk)
            ShopifyWebhookService.process(event.pk)
        handler.handle.assert_called_once()


@override_settings(SHOPIFY_APP_URL="https://erp.example.com")
@mock.patch.dict(WebhookHandlers.REGISTRY, {"app/uninstalled": mock.Mock(), "orders/create": mock.Mock()}, clear=True)
class ShopifyWebhookSubscriptionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = ShopifyStore.objects.create(company=make_company(name="Company A"), name="A", shop_domain="a.myshopify.com")

    @mock.patch("apps.shopify.client.ShopifyClient.graphql")
    def test_only_missing_topics_are_created(self, graphql):
        uri = "https://erp.example.com" + reverse("shopify_webhook")
        graphql.side_effect = [
            {"webhookSubscriptions": {"nodes": [
                {"topic": "APP_UNINSTALLED", "uri": uri},
                {"topic": "ORDERS_CREATE", "uri": "https://old-tunnel.example.com/hooks"},
            ]}},
            {"webhookSubscriptionCreate": {"userErrors": []}},
        ]
        self.assertEqual(ShopifyWebhookService.sync_subscriptions(self.store), ["orders/create"])
        self.assertEqual(graphql.call_args.args[1], {"topic": "ORDERS_CREATE", "uri": uri})

    @mock.patch("apps.shopify.client.ShopifyClient.graphql")
    def test_refused_topic_does_not_block_the_others(self, graphql):
        graphql.side_effect = [
            {"webhookSubscriptions": {"nodes": []}},
            {"webhookSubscriptionCreate": {"userErrors": []}},
            {"webhookSubscriptionCreate": {"userErrors": [{"field": ["topic"], "message": "Not approved for protected customer data"}]}},
        ]
        with self.assertRaises(ShopifyAPIError) as ctx:
            ShopifyWebhookService.sync_subscriptions(self.store)
        self.assertIn("orders/create", str(ctx.exception))
        self.assertEqual(graphql.call_count, 3)  # app/uninstalled was still created


class ShopifyWebhookEventAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company_a = make_company(name="Company A")
        company_b = make_company(name="Company B")
        cls.user = make_user(username="staff_a")
        CompanyUser.objects.create(user=cls.user, company=company_a, is_active=True)
        store_a = ShopifyStore.objects.create(company=company_a, name="A", shop_domain="a.myshopify.com")
        store_b = ShopifyStore.objects.create(company=company_b, name="B", shop_domain="b.myshopify.com")
        cls.event_a = ShopifyWebhookEvent.objects.create(store=store_a, webhook_id="a", topic="orders/create", payload={})
        ShopifyWebhookEvent.objects.create(store=store_b, webhook_id="b", topic="orders/create", payload={})

    def test_only_own_company_events_are_listed(self):
        cache.clear()
        request = RequestFactory().get("/")
        request.user = self.user
        admin = ShopifyWebhookEventAdmin(ShopifyWebhookEvent, admin_site=None)
        self.assertEqual(list(admin.get_queryset(request)), [self.event_a])


class WebhookRetentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = ShopifyStore.objects.create(company=make_company(name="Fistex"), name="Shop", shop_domain="f.myshopify.com")

    def event(self, webhook_id, status, days_old):
        event = ShopifyWebhookEvent.objects.create(
            store=self.store, webhook_id=webhook_id, topic="orders/create", payload={"email": "ana@example.com"}, status=status
        )
        ShopifyWebhookEvent.objects.filter(pk=event.pk).update(received_at=timezone.now() - timedelta(days=days_old))
        return event

    def test_handled_deliveries_are_deleted_after_the_retention_period_and_unhandled_ones_kept(self):
        self.event("old-processed", WebhookEventStatus.PROCESSED, 31)
        self.event("old-ignored", WebhookEventStatus.IGNORED, 31)
        self.event("old-failed", WebhookEventStatus.FAILED, 31)
        self.event("old-received", WebhookEventStatus.RECEIVED, 31)
        self.event("recent", WebhookEventStatus.PROCESSED, 29)

        self.assertEqual(ShopifyWebhookService.delete_old_events(batch_size=1), 2)
        self.assertEqual(
            set(ShopifyWebhookEvent.objects.values_list("webhook_id", flat=True)), {"old-failed", "old-received", "recent"}
        )
