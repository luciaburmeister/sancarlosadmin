"""
Pushing ERP stock to a store's Shopify locations.

Each mapped location shows its warehouse's sellable quantity
(``InventoryItemQuerySet.with_sellable_quantity``) as Shopify's ``on_hand``.
Shopify subtracts its own unfulfilled orders from that, so the reservations of
the store's imported orders are added back rather than counted twice.
Updates are compare-and-set: if Shopify's number changed since it was read,
the batch is read again and retried once.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import QuerySet
from django.utils.translation import gettext as _

from apps.shopify.client import ShopifyClient
from apps.shopify.models import ShopifyLocationMap, ShopifyStore, ShopifyVariantLink, VariantLinkStatus
from apps.shopify.services.base import ShopifyService

if TYPE_CHECKING:
    from inventory.models import Warehouse


@dataclass
class StockPushResult:
    updated: int = 0
    unchanged: int = 0
    not_tracked: int = 0


class ShopifyStockService(ShopifyService):
    BATCH_SIZE = 250

    LEVELS_QUERY = """
    query ($ids: [ID!]!, $locationId: ID!) {
      nodes(ids: $ids) {
        ... on InventoryItem {
          id
          tracked
          inventoryLevel(locationId: $locationId) { quantities(names: ["on_hand"]) { name quantity } }
        }
      }
    }
    """
    ACTIVATE = """
    mutation ($inventoryItemId: ID!, $locationId: ID!, $idempotencyKey: String!) {
      inventoryActivate(inventoryItemId: $inventoryItemId, locationId: $locationId) @idempotent(key: $idempotencyKey) {
        userErrors { field message }
      }
    }
    """
    SET_QUANTITIES = """
    mutation ($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
      inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
        userErrors { code field message }
      }
    }
    """

    @classmethod
    def push(cls, store: ShopifyStore, item_ids: Iterable[int] | None = None) -> StockPushResult:
        """Set each linked variant's stock at every mapped location.

        ``item_ids`` limits the push to those ERP items; ``None`` pushes all.
        """
        result = StockPushResult()
        links = cls._links(store, item_ids)
        locations = store.locations.filter(is_active=True, warehouse__isnull=False).select_related("warehouse")
        if not links:
            return result
        client = ShopifyClient(store)
        # Shopify already holds back what its own unshipped orders reserve.
        own_quotes = store.orders.filter(quote__isnull=False).values("quote_id")
        for location in locations:
            sellable = cls._sellable(links, location.warehouse, own_quotes)
            for start in range(0, len(links), cls.BATCH_SIZE):
                cls._push_batch(client, store, location, links[start:start + cls.BATCH_SIZE], sellable, result)
        return result

    @staticmethod
    def _links(store: ShopifyStore, item_ids: Iterable[int] | None) -> list[ShopifyVariantLink]:
        from inventory.choices import ItemType

        links = store.variant_links.filter(
            status=VariantLinkStatus.MATCHED,
            # Kits hold no stock of their own, and services none at all.
            item__has_components=False,
            item__item_type__in=[ItemType.STOCKABLE, ItemType.FINISHED_PRODUCT, ItemType.OWN_PRODUCTION],
        ).exclude(inventory_item_id="")
        if item_ids is not None:
            links = links.filter(item_id__in=item_ids)
        return list(links.order_by("pk"))

    @staticmethod
    def _sellable(links: list[ShopifyVariantLink], warehouse: Warehouse, own_quotes: QuerySet) -> dict[int, int]:
        from inventory.models import InventoryItem

        rows = (
            InventoryItem.objects.filter(pk__in={link.item_id for link in links})
            .with_sellable_quantity(warehouse, channel_quote_ids=own_quotes)
            .values_list("pk", "sellable_quantity")
        )
        # Shopify counts whole units; a partly reserved unit is not offered.
        return {pk: math.floor(quantity) for pk, quantity in rows}

    @classmethod
    def _push_batch(
        cls,
        client: ShopifyClient,
        store: ShopifyStore,
        location: ShopifyLocationMap,
        links: list[ShopifyVariantLink],
        sellable: dict[int, int],
        result: StockPushResult,
    ) -> None:
        for attempt in (1, 2):
            levels = cls._read_levels(client, links, location)
            changes, unchanged, not_tracked = [], 0, 0
            for link in links:
                tracked, on_hand = levels.get(link.inventory_item_id, (False, None))
                if not tracked:
                    not_tracked += 1
                    continue
                if on_hand is None:
                    cls._activate(client, link.inventory_item_id, location.location_id)
                    on_hand = 0
                desired = sellable.get(link.item_id, 0)
                if desired == on_hand:
                    unchanged += 1
                    continue
                changes.append({
                    "inventoryItemId": link.inventory_item_id,
                    "locationId": location.location_id,
                    "quantity": desired,
                    "changeFromQuantity": on_hand,
                })
            errors = cls._set_quantities(client, store, changes) if changes else []
            if not errors:
                result.updated += len(changes)
                result.unchanged += unchanged
                result.not_tracked += not_tracked
                return
            # Usually a stock change in Shopify between the read and the
            # write: read again and retry once.
            if attempt == 2:
                cls.raise_user_errors(errors, _("Shopify refused the stock update: %(detail)s"))

    @classmethod
    def _read_levels(
        cls, client: ShopifyClient, links: list[ShopifyVariantLink], location: ShopifyLocationMap
    ) -> dict[str, tuple[bool, int | None]]:
        nodes = client.graphql(
            cls.LEVELS_QUERY, {"ids": [link.inventory_item_id for link in links], "locationId": location.location_id}
        )["nodes"]
        levels = {}
        for node in nodes:
            if not node:
                continue  # deleted in Shopify since the last catalog sync
            level = node.get("inventoryLevel")
            on_hand = None
            if level is not None:
                on_hand = next((q["quantity"] for q in level["quantities"] if q["name"] == "on_hand"), 0)
            levels[node["id"]] = (bool(node.get("tracked")), on_hand)
        return levels

    @classmethod
    def _activate(cls, client: ShopifyClient, inventory_item_id: str, location_id: str) -> None:
        """Stock the item at the location, which Shopify needs before setting it."""
        variables = {"inventoryItemId": inventory_item_id, "locationId": location_id, "idempotencyKey": uuid.uuid4().hex}
        errors = client.graphql(cls.ACTIVATE, variables)["inventoryActivate"]["userErrors"]
        cls.raise_user_errors(errors, _("Shopify could not stock the item at the location: %(detail)s"))

    @classmethod
    def _set_quantities(cls, client: ShopifyClient, store: ShopifyStore, changes: list[dict]) -> list[dict]:
        return client.graphql(
            cls.SET_QUANTITIES,
            {
                "input": {
                    "name": "on_hand",
                    "reason": "correction",
                    "referenceDocumentUri": f"erp://shopify-stock-sync/store/{store.pk}",
                    "quantities": changes,
                },
                "idempotencyKey": uuid.uuid4().hex,
            },
        )["inventorySetQuantities"]["userErrors"]
