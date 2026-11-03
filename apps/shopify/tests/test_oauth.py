import hashlib
import hmac
import time
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api.fleet.tests.helpers import make_company, make_user
from apps.shopify.client import ShopifyClient
from apps.shopify.exceptions import ShopifyAPIError, ShopifyAuthError
from apps.shopify.models import ShopifyApp, ShopifyStore
from apps.shopify.services.oauth import ShopifyOAuthService
from company.models import CompanyUser

SECRET = "app-client-secret"
TOKEN_RESPONSE = {
    "access_token": "shpat_new",
    "scope": "read_products,write_inventory",
    "expires_in": 3600,
    "refresh_token": "shprt_new",
    "refresh_token_expires_in": 7776000,
}


def shopify_response(status=200, payload=None):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload if payload is not None else {}
    return response


def signed(params, secret=SECRET):
    message = "&".join(f"{key}={params[key]}" for key in sorted(params))
    return {**params, "hmac": hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()}


@override_settings(SHOPIFY_APP_URL="https://erp.example.com")
class ShopifyOAuthServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Company A")
        cls.app = ShopifyApp(name="Pilot app", client_id="client-123")
        cls.app.set_client_secret(SECRET)
        cls.app.save()

    def setUp(self):
        self.store = ShopifyStore.objects.create(
            company=self.company, app=self.app, name="A", shop_domain="a.myshopify.com"
        )

    def test_begin_builds_approval_url_and_remembers_the_attempt(self):
        session = {}
        url = urlparse(ShopifyOAuthService.begin(session, self.store))
        query = parse_qs(url.query)
        self.assertEqual(url.netloc, "a.myshopify.com")
        self.assertEqual(query["client_id"], ["client-123"])
        self.assertEqual(query["redirect_uri"], ["https://erp.example.com" + reverse("shopify_oauth_callback")])
        self.assertEqual(query["state"], [session[ShopifyOAuthService.SESSION_KEY]["state"]])
        self.assertEqual(session[ShopifyOAuthService.SESSION_KEY]["store_id"], self.store.pk)

    def test_begin_needs_an_app_and_the_public_url(self):
        self.store.app = None
        with self.assertRaises(ShopifyAuthError):
            ShopifyOAuthService.begin({}, self.store)
        self.store.app = self.app
        with self.settings(SHOPIFY_APP_URL=""), self.assertRaises(ShopifyAuthError):
            ShopifyOAuthService.begin({}, self.store)

    def test_pending_attempt_is_single_use_and_expires(self):
        session = {ShopifyOAuthService.SESSION_KEY: {"state": "abc", "store_id": 7, "started_at": time.time()}}
        self.assertIsNone(ShopifyOAuthService.take_pending_store_id(dict(session), "other"))
        self.assertEqual(ShopifyOAuthService.take_pending_store_id(session, "abc"), 7)
        self.assertIsNone(ShopifyOAuthService.take_pending_store_id(session, "abc"))
        old = {ShopifyOAuthService.SESSION_KEY: {"state": "abc", "store_id": 7, "started_at": time.time() - 3600}}
        self.assertIsNone(ShopifyOAuthService.take_pending_store_id(old, "abc"))

    def test_callback_signature(self):
        params = signed({"code": "c", "shop": "a.myshopify.com", "state": "s", "timestamp": "1"})
        self.assertTrue(ShopifyOAuthService.is_valid_callback(params, SECRET))
        self.assertFalse(ShopifyOAuthService.is_valid_callback({**params, "shop": "b.myshopify.com"}, SECRET))
        self.assertFalse(ShopifyOAuthService.is_valid_callback(params, "another-secret"))
        self.assertFalse(ShopifyOAuthService.is_valid_callback({"code": "c"}, SECRET))

    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_complete_connection_stores_expiring_tokens(self, post):
        post.return_value = shopify_response(payload=TOKEN_RESPONSE)
        ShopifyOAuthService.complete_connection(self.store, "the-code")

        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["code"], sent["expiring"], sent["client_secret"]), ("the-code", "1", SECRET))
        self.store.refresh_from_db()
        self.assertTrue(self.store.is_connected)
        self.assertEqual(self.store.get_access_token(), "shpat_new")
        self.assertEqual(self.store.scopes, "read_products,write_inventory")
        self.assertIsNotNone(self.store.connected_at)
        self.assertGreater(self.store.access_token_expires_at, timezone.now() + timedelta(minutes=50))

    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_refused_exchange_leaves_store_disconnected(self, post):
        post.return_value = shopify_response(status=400)
        with self.assertRaises(ShopifyAuthError):
            ShopifyOAuthService.complete_connection(self.store, "bad-code")
        self.store.refresh_from_db()
        self.assertFalse(self.store.is_connected)

    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_expiring_token_is_refreshed(self, post):
        self.store.set_tokens(
            access_token="old", access_token_expires_at=timezone.now() + timedelta(minutes=1),
            refresh_token="shprt_old", refresh_token_expires_at=timezone.now() + timedelta(days=30),
        )
        self.store.save()
        post.return_value = shopify_response(payload=TOKEN_RESPONSE)

        store = ShopifyOAuthService.ensure_fresh_token(self.store)

        sent = post.call_args.kwargs["data"]
        self.assertEqual((sent["grant_type"], sent["refresh_token"]), ("refresh_token", "shprt_old"))
        self.assertEqual(store.get_access_token(), "shpat_new")
        self.assertEqual(store.get_refresh_token(), "shprt_new")

    def expiring_store(self):
        self.store.set_tokens(
            access_token="old", access_token_expires_at=timezone.now() + timedelta(minutes=1),
            refresh_token="shprt_old", refresh_token_expires_at=timezone.now() + timedelta(days=30),
        )
        self.store.save()

    @mock.patch("apps.shopify.services.oauth.time.sleep")
    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_refresh_is_tried_again_while_shopify_is_briefly_unavailable(self, post, sleep):
        self.expiring_store()
        post.side_effect = [shopify_response(status=504), requests.ConnectionError(), shopify_response(payload=TOKEN_RESPONSE)]

        store = ShopifyOAuthService.ensure_fresh_token(self.store)

        self.assertEqual(store.get_access_token(), "shpat_new")
        self.assertEqual(post.call_count, 3)

    @mock.patch("apps.shopify.services.oauth.time.sleep")
    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_refresh_gives_up_and_never_retries_what_shopify_may_have_done(self, post, sleep):
        self.expiring_store()
        for case, answers, calls in [
            ("still unavailable", [shopify_response(status=503)] * 3, 3),
            ("refused", [shopify_response(status=400)], 1),
            # A timeout may come after Shopify issued tokens and spent the refresh token.
            ("timeout", [requests.ReadTimeout()], 1),
        ]:
            with self.subTest(case):
                post.reset_mock()
                post.side_effect = answers

                with self.assertRaises(ShopifyAuthError):
                    ShopifyOAuthService.ensure_fresh_token(self.store)

                self.assertEqual(post.call_count, calls)
                self.assertEqual(ShopifyStore.objects.get(pk=self.store.pk).get_access_token(), "old")

    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_fresh_token_is_not_refreshed(self, post):
        self.store.set_tokens(
            access_token="current", access_token_expires_at=timezone.now() + timedelta(hours=1),
            refresh_token="r", refresh_token_expires_at=None,
        )
        self.assertIs(ShopifyOAuthService.ensure_fresh_token(self.store), self.store)
        post.assert_not_called()


@override_settings(SHOPIFY_APP_URL="https://erp.example.com")
class ShopifyOAuthCallbackViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company_a = make_company(name="Company A")
        cls.company_b = make_company(name="Company B")
        cls.app = ShopifyApp(name="Pilot app", client_id="client-123")
        cls.app.set_client_secret(SECRET)
        cls.app.save()
        cls.store_a = ShopifyStore.objects.create(company=cls.company_a, app=cls.app, name="A", shop_domain="a.myshopify.com")
        cls.store_b = ShopifyStore.objects.create(company=cls.company_b, app=cls.app, name="B", shop_domain="b.myshopify.com")
        cls.user = make_user(username="staff_a")
        cls.user.is_staff = True
        cls.user.save()
        cls.user.user_permissions.add(Permission.objects.get(codename="change_shopifystore"))
        CompanyUser.objects.create(user=cls.user, company=cls.company_a, is_active=True)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def start(self, store, state="state-1"):
        session = self.client.session
        session[ShopifyOAuthService.SESSION_KEY] = {"state": state, "store_id": store.pk, "started_at": time.time()}
        session.save()

    def callback(self, **params):
        return self.client.get(reverse("shopify_oauth_callback"), params)

    @mock.patch("apps.shopify.views.ShopifyWebhookService.sync_subscriptions")
    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_valid_callback_connects_the_store_and_subscribes_webhooks(self, post, sync):
        post.return_value = shopify_response(payload=TOKEN_RESPONSE)
        self.start(self.store_a)
        response = self.callback(**signed({"code": "c", "shop": "a.myshopify.com", "state": "state-1", "timestamp": "1"}))

        self.assertRedirects(response, reverse("admin:shopify_shopifystore_change", args=[self.store_a.pk]), fetch_redirect_response=False)
        self.store_a.refresh_from_db()
        self.assertTrue(self.store_a.is_connected)
        sync.assert_called_once()

    @mock.patch("apps.shopify.views.ShopifyWebhookService.sync_subscriptions", side_effect=ShopifyAPIError("down"))
    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_failed_webhook_setup_keeps_the_connection(self, post, sync):
        post.return_value = shopify_response(payload=TOKEN_RESPONSE)
        self.start(self.store_a)
        self.callback(**signed({"code": "c", "shop": "a.myshopify.com", "state": "state-1"}))
        self.store_a.refresh_from_db()
        self.assertTrue(self.store_a.is_connected)

    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_forged_or_mismatched_callbacks_connect_nothing(self, post):
        cases = {
            "bad signature": {**signed({"code": "c", "shop": "a.myshopify.com", "state": "state-1"}), "hmac": "0" * 64},
            "other shop": signed({"code": "c", "shop": "b.myshopify.com", "state": "state-1"}),
            "wrong state": signed({"code": "c", "shop": "a.myshopify.com", "state": "guessed"}),
        }
        for label, params in cases.items():
            with self.subTest(label):
                self.start(self.store_a)
                self.callback(**params)
                self.store_a.refresh_from_db()
                self.assertFalse(self.store_a.is_connected)
        post.assert_not_called()

    @mock.patch("apps.shopify.services.oauth.requests.post")
    def test_another_companys_store_cannot_be_connected(self, post):
        self.start(self.store_b)
        response = self.callback(**signed({"code": "c", "shop": "b.myshopify.com", "state": "state-1"}))
        self.assertEqual(response.status_code, 404)
        post.assert_not_called()

    def test_anonymous_user_is_sent_to_login(self):
        self.client.logout()
        response = self.callback(state="state-1")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])


class ShopifyClientTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Company A")
        cls.app = ShopifyApp.objects.create(name="Pilot app", client_id="client-123")

    def setUp(self):
        self.store = ShopifyStore(company=self.company, app=self.app, name="A", shop_domain="a.myshopify.com")
        self.store.set_tokens(access_token="shpat", access_token_expires_at=None, refresh_token="", refresh_token_expires_at=None)
        self.store.save()

    @mock.patch("apps.shopify.client.time.sleep")
    @mock.patch("apps.shopify.client.requests.post")
    def test_throttled_request_is_retried(self, post, sleep):
        throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]}
        post.side_effect = [shopify_response(payload=throttled), shopify_response(payload={"data": {"shop": {"name": "A"}}})]
        self.assertEqual(ShopifyClient(self.store).graphql("{ shop { name } }"), {"shop": {"name": "A"}})
        self.assertEqual(post.call_args.kwargs["headers"]["X-Shopify-Access-Token"], "shpat")

    @mock.patch("apps.shopify.client.time.sleep")
    @mock.patch("apps.shopify.client.requests.post")
    def test_queries_are_tried_again_while_shopify_is_briefly_unavailable(self, post, sleep):
        post.side_effect = [
            shopify_response(status=502), requests.ConnectionError(), shopify_response(payload={"data": {"shop": {"name": "A"}}}),
        ]

        self.assertEqual(ShopifyClient(self.store).graphql("{ shop { name } }"), {"shop": {"name": "A"}})
        self.assertEqual(post.call_count, 3)

    @mock.patch("apps.shopify.client.time.sleep")
    @mock.patch("apps.shopify.client.requests.post")
    def test_mutations_are_not_repeated_after_a_temporary_error(self, post, sleep):
        # Shopify may have applied the mutation before failing to answer.
        for answer in (shopify_response(status=504), requests.ConnectionError()):
            with self.subTest(answer=answer):
                post.reset_mock()
                post.side_effect = [answer]

                with self.assertRaises(ShopifyAPIError):
                    ShopifyClient(self.store).graphql("mutation { productCreate(product: {title: \"x\"}) { userErrors { message } } }")

                self.assertEqual(post.call_count, 1)

    @mock.patch("apps.shopify.client.requests.post")
    def test_reply_without_data_is_an_api_error(self, post):
        post.return_value = shopify_response(payload={"unexpected": True})
        with self.assertRaises(ShopifyAPIError):
            ShopifyClient(self.store).graphql("{ shop { name } }")

    @mock.patch("apps.shopify.client.requests.post")
    def test_errors_are_raised(self, post):
        post.return_value = shopify_response(status=401)
        with self.assertRaises(ShopifyAuthError):
            ShopifyClient(self.store).graphql("{ shop { name } }")
        post.return_value = shopify_response(payload={"errors": [{"message": "Field 'x' doesn't exist"}]})
        with self.assertRaises(ShopifyAPIError):
            ShopifyClient(self.store).graphql("{ x }")
