"""
Quote requests sent from a store's product pages.

The storefront posts them through Shopify's app proxy (``/apps/<subpath>/
quote-request`` on the store's own domain), which forwards them to the ERP
signed with the app's client secret. A request becomes a draft quote for the
requester's client, priced by the ERP like a quote made by hand; staff take it
from there with the ERP's normal quote flow.

Storefront side, per store: set up the app's proxy in the Dev Dashboard
(prefix ``apps``, a subpath, proxy URL ``<ERP>/integrations/shopify/proxy``)
and have the theme post ``QuoteRequestForm``'s fields to it. The catalog push
tags products ``request-quote`` (show the form) and ``quote-only`` (also hide
the price and checkout).
"""

import hashlib
import hmac
import time

from django.http import QueryDict
from django.utils.translation import gettext as _

from apps.shopify.models import ShopifyStore, VariantLinkStatus
from inventory.choices import OnlineSaleMode
from sales.models import Quote
from sales.services.external_order import ExternalCustomer, ExternalOrderService


class QuoteRequestError(Exception):
    """The request can't become a quote; the message is safe to show the customer."""


class ShopifyQuoteRequestService:
    # Shopify signs each proxied request as it forwards it, so a valid one is always fresh.
    MAX_AGE_SECONDS = 300

    @classmethod
    def is_authentic(cls, query: QueryDict, client_secret: str, now: float | None = None) -> bool:
        """Check the app proxy signature: a hex HMAC-SHA256 of the sorted query parameters."""
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

    @staticmethod
    def receive(store: ShopifyStore, data: dict, customer_id: str = "") -> Quote:
        """The draft quote for a validated request (``QuoteRequestForm`` data)."""
        from apps.shopify.services.orders import ShopifyOrderService

        link = (
            store.variant_links.filter(
                variant_id=f"gid://shopify/ProductVariant/{data['variant_id']}",
                status=VariantLinkStatus.MATCHED,
                item__sell_online=True,
            )
            .select_related("item")
            .first()
        )
        if link is None or store.sale_mode(link.item) == OnlineSaleMode.BUY:
            raise QuoteRequestError(_("This product can't be quoted."))
        # Always the requester's own client, whatever the store does for orders:
        # the quote is sent back to them.
        client = ShopifyOrderService.customer_client(
            store,
            f"gid://shopify/Customer/{customer_id}" if customer_id else None,
            ExternalCustomer(name=data["name"], email=data["email"], phone=data["phone"]),
        )
        return ExternalOrderService.request_quote(
            store.company,
            client,
            [(link.item, data["quantity"])],
            name=_("Web quote request: %(product)s") % {"product": link.item.name},
            notes=data["notes"],
        )
