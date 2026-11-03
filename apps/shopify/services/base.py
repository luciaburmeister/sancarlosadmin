from abc import ABC
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.utils import translation

from apps.shopify.client import ShopifyClient
from apps.shopify.exceptions import ShopifyAPIError, ShopifyBusy


class ShopifyService(ABC):
    """Base of the services that work with a store's Shopify data.

    Holds what they all share: reading Shopify's amounts, its mutation errors
    and its paginated lists, the shop's tax setting, writing metafields, the
    language of the text it stores, and the lock that keeps one long job per
    store from running twice.
    """

    CENT = Decimal("0.01")
    # GraphQL fields of a MoneyBag, in the shop's currency.
    MONEY = "shopMoney { amount }"
    PAGE_SIZE = 250
    SHOP_TAXES_QUERY = "{ shop { taxesIncluded } }"
    # Metafields the ERP writes (on products and customers) and themes read.
    METAFIELD_NAMESPACE = "polipus"
    METAFIELD_BATCH = 25  # Shopify's limit per metafieldsSet or metafieldsDelete call
    METAFIELDS_SET = """
    mutation ($metafields: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $metafields) { userErrors { field message } } }
    """
    METAFIELDS_DELETE = """
    mutation ($metafields: [MetafieldIdentifierInput!]!) {
      metafieldsDelete(metafields: $metafields) { userErrors { field message } }
    }
    """

    @staticmethod
    def erp_text(message: str, **params: object) -> str:
        """``message`` in the ERP's language, with ``params`` filled in.

        For text the integration stores in ERP records or sends to Shopify
        (titles, notes, references): it must not depend on who triggered the
        run, a customer's browser or a staff member's admin language. Mark the
        message with ``gettext_noop`` so it is extracted for translation.
        """
        with translation.override(settings.LANGUAGE_CODE):
            text = translation.gettext(message)
        return text % params if params else text

    @classmethod
    def taxes_included(cls, client: ShopifyClient) -> bool:
        """Whether the shop's prices include tax; read each run, as staff can change it."""
        return client.graphql(cls.SHOP_TAXES_QUERY)["shop"]["taxesIncluded"]

    @classmethod
    def set_metafields(cls, client: ShopifyClient, metafields: list[dict], message: str) -> None:
        """Write ``metafields`` (``ownerId``, ``key``, ``type``, ``value``) in the ERP's namespace."""
        for start in range(0, len(metafields), cls.METAFIELD_BATCH):
            batch = [{**metafield, "namespace": cls.METAFIELD_NAMESPACE} for metafield in metafields[start:start + cls.METAFIELD_BATCH]]
            data = client.graphql(cls.METAFIELDS_SET, {"metafields": batch})
            cls.raise_user_errors(data["metafieldsSet"]["userErrors"], message)

    @classmethod
    def delete_metafields(cls, client: ShopifyClient, metafields: list[dict], message: str) -> None:
        """Delete ``metafields`` (``ownerId``, ``key``) from the ERP's namespace; missing ones are fine."""
        for start in range(0, len(metafields), cls.METAFIELD_BATCH):
            batch = [{**metafield, "namespace": cls.METAFIELD_NAMESPACE} for metafield in metafields[start:start + cls.METAFIELD_BATCH]]
            data = client.graphql(cls.METAFIELDS_DELETE, {"metafields": batch})
            cls.raise_user_errors(data["metafieldsDelete"]["userErrors"], message)

    @staticmethod
    @contextmanager
    def store_lock(key: str, seconds: int, busy: ShopifyBusy) -> Iterator[Callable[[], None]]:
        """Hold the cache lock ``key`` for one job, or raise ``busy`` if another run holds it.

        Yields a function that keeps the lock for another ``seconds``, for
        jobs that run longer than that.
        """
        if not cache.add(key, True, seconds):
            raise busy
        try:
            yield lambda: cache.touch(key, seconds)
        finally:
            cache.delete(key)

    GID = "gid://shopify/{resource}/{id}"

    @classmethod
    def gid(cls, resource: str, legacy_id: int | str) -> str:
        """A Shopify global ID from a numeric one, as storefronts and app proxies give them."""
        return cls.GID.format(resource=resource, id=legacy_id)

    @staticmethod
    def money(node: dict) -> Decimal:
        """The shop-currency amount of a MoneyBag."""
        return Decimal(str(node["shopMoney"]["amount"]))

    @staticmethod
    def raise_user_errors(user_errors: list[dict], message: str) -> None:
        """Raise the first ``userErrors`` entry of a mutation, if any.

        ``message`` is the translated text, with ``%(detail)s`` for Shopify's own.
        """
        if user_errors:
            raise ShopifyAPIError(message % {"detail": user_errors[0]["message"]})

    @classmethod
    def paginate(cls, client: ShopifyClient, query: str, variables: dict, path: tuple[str, ...]) -> list[dict]:
        """Every node of a paginated connection, following ``path`` into the response."""
        nodes, after = [], None
        while True:
            data = client.graphql(query, {**variables, "first": cls.PAGE_SIZE, "after": after})
            for key in path:
                data = data[key]
                if data is None:
                    return nodes
            nodes.extend(data["nodes"])
            if not data["pageInfo"]["hasNextPage"]:
                return nodes
            after = data["pageInfo"]["endCursor"]
