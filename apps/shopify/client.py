"""Minimal client for one connected store's GraphQL Admin API."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import requests
from django.utils.translation import gettext as _

from apps.shopify.config import ShopifyApiConfig
from apps.shopify.exceptions import ShopifyAPIError, ShopifyAuthError
from apps.shopify.services.oauth import ShopifyOAuthService

if TYPE_CHECKING:
    from apps.shopify.models import ShopifyStore


class ShopifyClient:
    """Sends GraphQL queries for one store, refreshing its token as needed.

    Throttled requests (HTTP 429 or a THROTTLED error) are retried with a short
    backoff; anything else raises ``ShopifyAPIError``, or ``ShopifyAuthError``
    when the store must be connected again.
    """

    MAX_ATTEMPTS = 3
    REQUEST_TIMEOUT_SECONDS = 30

    def __init__(self, store: ShopifyStore) -> None:
        self.store = store

    @property
    def endpoint(self) -> str:
        return f"https://{self.store.shop_domain}/admin/api/{ShopifyApiConfig.API_VERSION}/graphql.json"

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self.store = ShopifyOAuthService.ensure_fresh_token(self.store)
            try:
                response = requests.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": self.store.get_access_token()},
                    timeout=self.REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                raise ShopifyAPIError(_("Could not reach Shopify.")) from exc

            if response.status_code == 401:
                raise ShopifyAuthError(_("Shopify rejected the store's token. Connect it again."))
            try:
                body = response.json()
            except ValueError:
                body = {}
            throttled = response.status_code == 429 or any(
                (error.get("extensions") or {}).get("code") == "THROTTLED" for error in body.get("errors") or []
            )
            if throttled and attempt < self.MAX_ATTEMPTS:
                time.sleep(attempt)
                continue
            if response.status_code >= 400 or body.get("errors") or not isinstance(body.get("data"), dict):
                raise ShopifyAPIError(self._error_message(response.status_code, body))
            return body["data"]
        raise ShopifyAPIError(_("Shopify kept throttling the request."))

    @staticmethod
    def _error_message(status: int, body: dict) -> str:
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            detail = errors[0].get("message", "")
        else:
            detail = str(errors or "")
        return _("Shopify API error (HTTP %(status)s): %(detail)s") % {"status": status, "detail": detail}
