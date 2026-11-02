"""
Connecting a store through Shopify's authorization code grant, and keeping its
expiring offline access token fresh.

https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/authorization-code-grant
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from django.contrib.sessions.backends.base import SessionBase
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.shopify.config import ShopifyApiConfig
from apps.shopify.exceptions import ShopifyAuthError
from apps.shopify.models import ShopifyStore


class ShopifyOAuthService:
    SESSION_KEY = "shopify_oauth"
    # How long a started connection may wait for Shopify's redirect back.
    STATE_MAX_AGE_SECONDS = 600
    REQUEST_TIMEOUT_SECONDS = 15
    # Refresh this long before expiry, so a request never leaves with a token
    # that dies on the way.
    REFRESH_MARGIN = timedelta(minutes=5)
    TOKEN_FIELDS = (
        "access_token_encrypted",
        "access_token_expires_at",
        "refresh_token_encrypted",
        "refresh_token_expires_at",
    )

    @staticmethod
    def redirect_uri() -> str:
        return ShopifyApiConfig.public_url("shopify_oauth_callback")

    @classmethod
    def begin(cls, session: SessionBase, store: ShopifyStore) -> str:
        """Remember a new connection attempt and return Shopify's approval URL."""
        if store.app_id is None:
            raise ShopifyAuthError(_("Choose the Shopify app for this store first."))
        state = secrets.token_urlsafe(32)
        query = urlencode({
            "client_id": store.app.client_id,
            "scope": ",".join(ShopifyApiConfig.SCOPES),
            "redirect_uri": cls.redirect_uri(),
            "state": state,
        })
        session[cls.SESSION_KEY] = {"state": state, "store_id": store.pk, "started_at": time.time()}
        return f"https://{store.shop_domain}/admin/oauth/authorize?{query}"

    @classmethod
    def take_pending_store_id(cls, session: SessionBase, state: str) -> int | None:
        """The store id of the attempt matching ``state``, consumed so it can't be replayed."""
        pending = session.pop(cls.SESSION_KEY, None)
        if not pending or not state or not hmac.compare_digest(pending["state"], state):
            return None
        if time.time() - pending["started_at"] > cls.STATE_MAX_AGE_SECONDS:
            return None
        return pending["store_id"]

    @staticmethod
    def is_valid_callback(params: dict, client_secret: str) -> bool:
        """Check Shopify's HMAC over the callback's query parameters."""
        received = params.get("hmac", "")
        message = "&".join(f"{key}={params[key]}" for key in sorted(params) if key != "hmac")
        expected = hmac.new(client_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        return bool(received) and hmac.compare_digest(expected, received)

    @classmethod
    @transaction.atomic
    def complete_connection(cls, store: ShopifyStore, code: str) -> None:
        """Exchange the callback's code for an expiring offline token pair."""
        data = cls._token_request(store, {"code": code, "expiring": "1"})
        cls._apply_tokens(store, data)
        store.scopes = data.get("scope", "")
        store.connected_at = timezone.now()
        store.save(update_fields=[*cls.TOKEN_FIELDS, "scopes", "connected_at", "updated_at"])

    @classmethod
    def ensure_fresh_token(cls, store: ShopifyStore) -> ShopifyStore:
        """Return the store with an access token that is not about to expire."""
        if not cls._needs_refresh(store):
            return store
        with transaction.atomic():
            locked = ShopifyStore.objects.select_related("app").select_for_update(of=("self",)).get(pk=store.pk)
            # Another worker may have refreshed it while this one waited.
            if cls._needs_refresh(locked):
                refresh_token = locked.get_refresh_token()
                if not refresh_token:
                    raise ShopifyAuthError(_("The store's connection expired. Connect it again."))
                data = cls._token_request(locked, {"grant_type": "refresh_token", "refresh_token": refresh_token})
                cls._apply_tokens(locked, data)
                locked.save(update_fields=[*cls.TOKEN_FIELDS, "updated_at"])
        return locked

    @classmethod
    def _needs_refresh(cls, store: ShopifyStore) -> bool:
        expires_at = store.access_token_expires_at
        return expires_at is not None and expires_at - cls.REFRESH_MARGIN <= timezone.now()

    @staticmethod
    def _apply_tokens(store: ShopifyStore, data: dict) -> None:
        if not data.get("access_token"):
            raise ShopifyAuthError(_("Shopify returned no access token."))
        now = timezone.now()

        def expiry(seconds: int | str | None) -> datetime | None:
            return now + timedelta(seconds=int(seconds)) if seconds else None

        store.set_tokens(
            access_token=data["access_token"],
            access_token_expires_at=expiry(data.get("expires_in")),
            refresh_token=data.get("refresh_token", ""),
            refresh_token_expires_at=expiry(data.get("refresh_token_expires_in")),
        )

    @classmethod
    def _token_request(cls, store: ShopifyStore, fields: dict) -> dict:
        if store.app_id is None:
            raise ShopifyAuthError(_("Choose the Shopify app for this store first."))
        payload = {"client_id": store.app.client_id, "client_secret": store.app.get_client_secret(), **fields}
        try:
            response = requests.post(
                f"https://{store.shop_domain}/admin/oauth/access_token",
                data=payload,
                headers={"Accept": "application/json"},
                timeout=cls.REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ShopifyAuthError(_("Could not reach Shopify.")) from exc
        # The body is never logged or shown: it carries the tokens.
        if response.status_code != 200:
            raise ShopifyAuthError(
                _("Shopify refused the connection (HTTP %(status)s).") % {"status": response.status_code}
            )
        return response.json()
