from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.shopify.enums import AccessScope
from apps.shopify.exceptions import ShopifyAuthError


class ShopifyApiConfig:
    """What every part of the integration shares about the Shopify app."""

    # The GraphQL queries are written for this version; update it together with them.
    API_VERSION = "2026-10"
    # What the integration reads and writes in a store. A store approves these
    # when it installs the app, and must connect again to grant any added later.
    SCOPES = (
        AccessScope.READ_PRODUCTS,
        AccessScope.WRITE_PRODUCTS,
        AccessScope.READ_INVENTORY,
        AccessScope.WRITE_INVENTORY,
        AccessScope.READ_LOCATIONS,
        AccessScope.READ_ORDERS,
        AccessScope.READ_CUSTOMERS,
        AccessScope.READ_MERCHANT_MANAGED_FULFILLMENT_ORDERS,
        AccessScope.WRITE_MERCHANT_MANAGED_FULFILLMENT_ORDERS,
    )

    @staticmethod
    def public_url(url_name: str) -> str:
        """The ERP's public address of ``url_name``, where Shopify reaches it."""
        base = settings.SHOPIFY_APP_URL.rstrip("/")
        if not base:
            raise ShopifyAuthError(_("SHOPIFY_APP_URL is not configured."))
        return base + reverse(url_name)
