"""
Requests from a store's pages through Shopify's app proxy.

The storefront calls ``/apps/<subpath>/...`` on the store's own domain and
Shopify forwards the call to ``<ERP>/integrations/shopify/proxy/...``, adding
the shop, a timestamp, the signed-in customer (``logged_in_customer_id``, empty
for a guest) and a signature of all of them made with the app's client secret.
A valid signature is what authenticates the store and the customer.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from django.http import QueryDict

from apps.shopify.services.base import ShopifyService


class ShopifyAppProxy:
    MAX_AGE_SECONDS = 300

    @staticmethod
    def customer_id(query: QueryDict) -> str:
        """The signed-in customer's ID as the ERP stores Shopify customers; empty for a guest."""
        customer = query.get("logged_in_customer_id", "")
        return ShopifyService.gid("Customer", customer) if customer else ""

    @classmethod
    def is_authentic(cls, query: QueryDict, client_secret: str, now: float | None = None) -> bool:
        """Check the app proxy signature: a hex HMAC-SHA256 of the sorted query parameters, made recently."""
        signature = query.get("signature", "")
        if not signature or not client_secret:
            return False
        message = "".join(
            f"{key}={','.join(query.getlist(key))}" for key in sorted(query) if key != "signature"
        )
        expected = hmac.new(client_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return False
        try:
            age = (now or time.time()) - int(query.get("timestamp", ""))
        except ValueError:
            return False
        return abs(age) <= cls.MAX_AGE_SECONDS
