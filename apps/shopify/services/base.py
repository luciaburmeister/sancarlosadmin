from abc import ABC
from decimal import Decimal

from apps.shopify.client import ShopifyClient
from apps.shopify.exceptions import ShopifyAPIError


class ShopifyService(ABC):
    """Base of the services that work with a store's Shopify data.

    Holds what they all share: reading Shopify's amounts, its mutation errors
    and its paginated lists.
    """

    CENT = Decimal("0.01")
    # GraphQL fields of a MoneyBag, in the shop's currency.
    MONEY = "shopMoney { amount }"
    PAGE_SIZE = 250

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
