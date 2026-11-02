class ShopifyError(Exception):
    """A Shopify request failed. The message is safe to show to staff."""


class ShopifyAuthError(ShopifyError):
    """The store's connection is missing, refused or must be made again."""


class ShopifyAPIError(ShopifyError):
    """A connected store's API call failed."""
