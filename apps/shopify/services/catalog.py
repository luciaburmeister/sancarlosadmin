"""
Linking a store's Shopify locations and product variants to ERP warehouses and
items, and creating products in Shopify for items sold online.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape, linebreaks
from django.utils.translation import gettext as _

from apps.shopify.client import ShopifyClient
from apps.shopify.enums import ProductStatus
from apps.shopify.exceptions import ShopifyAPIError, ShopifyError
from apps.shopify.models import ShopifyLocationMap, ShopifyStore, ShopifyVariantLink, VariantLinkStatus
from apps.shopify.services.base import ShopifyService
from apps.shopify.services.push_scheduler import ShopifyPushScheduler

if TYPE_CHECKING:
    from inventory.models import InventoryItem


class CatalogPushBusy(ShopifyError):
    """Another catalog push for the store is still running."""


class CatalogItemError(ShopifyError):
    """The ERP item can't be sold online as it is; the message says what to fix."""


@dataclass
class CatalogPushResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    archived: int = 0
    restored: int = 0
    errors: list[str] = field(default_factory=list)


class ShopifyCatalogService(ShopifyService):
    VARIANT_FIELDS = "id sku title product { id title } inventoryItem { id }"
    LOCATIONS_QUERY = """
    query { locations(first: 250, includeInactive: true) { nodes { id name isActive } } }
    """
    VARIANTS_QUERY = f"""
    query ($first: Int!, $after: String) {{
      productVariants(first: $first, after: $after) {{
        nodes {{ {VARIANT_FIELDS} }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """
    PRODUCT_VARIANTS_QUERY = f"""
    query ($id: ID!, $first: Int!, $after: String) {{
      product(id: $id) {{
        variants(first: $first, after: $after) {{
          nodes {{ {VARIANT_FIELDS} }}
          pageInfo {{ hasNextPage endCursor }}
        }}
      }}
    }}
    """
    SKU_QUERY = f"""
    query ($query: String!) {{
      productVariants(first: 5, query: $query) {{ nodes {{ {VARIANT_FIELDS} }} }}
    }}
    """
    PRODUCT_CREATE = """
    mutation ($product: ProductCreateInput!) {
      productCreate(product: $product) {
        product { id title variants(first: 1) { nodes { id sku title product { id title } inventoryItem { id } } } }
        userErrors { field message }
      }
    }
    """
    VARIANT_UPDATE = """
    mutation ($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        userErrors { field message }
      }
    }
    """
    SHOP_TAXES_QUERY = "{ shop { taxesIncluded } }"
    PRODUCT_STATUS_QUERY = """
    query ($id: ID!) { product(id: $id) { status variantsCount { count } } }
    """
    PRODUCT_UPDATE = """
    mutation ($product: ProductUpdateInput!) {
      productUpdate(product: $product) {
        product { id status }
        userErrors { field message }
      }
    }
    """
    TAGS_ADD = """
    mutation ($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { field message } } }
    """
    TAGS_REMOVE = """
    mutation ($id: ID!, $tags: [String!]!) { tagsRemove(id: $id, tags: $tags) { userErrors { field message } } }
    """
    # Product tags the storefront theme reads: show "Request a quote", hide
    # "Add to cart". The ERP only ever adds and removes these two; any other
    # tag is Shopify's.
    REQUEST_QUOTE_TAG = "request-quote"
    QUOTE_ONLY_TAG = "quote-only"
    # pushed_signature of a product the ERP just created: its first update also
    # sets how Shopify handles its stock and shipping.
    NEW_PRODUCT = "new"
    PUSH_LOCK = "shopify:catalog-push-lock:{store_id}"
    PUSH_LOCK_SECONDS = 900

    @staticmethod
    def sku_key(sku: str | None) -> str:
        """A SKU as matched against item codes: trimmed, case-insensitive."""
        return (sku or "").strip().upper()

    @classmethod
    def sync_locations(cls, store: ShopifyStore) -> int:
        """Record the store's locations, keeping any warehouse already chosen."""
        nodes = ShopifyClient(store).graphql(cls.LOCATIONS_QUERY)["locations"]["nodes"]
        for node in nodes:
            ShopifyLocationMap.objects.update_or_create(
                store=store,
                location_id=node["id"],
                defaults={"name": node["name"], "is_active": node["isActive"]},
            )
        return len(nodes)

    @classmethod
    def sync_catalog(cls, store: ShopifyStore) -> Counter:
        """Read every variant of the store and link it by SKU. Returns status counts."""
        variants = cls.paginate(ShopifyClient(store), cls.VARIANTS_QUERY, {}, ("productVariants",))
        with transaction.atomic():
            cls._save_variants(store, variants)
            # A full read is authoritative: variants missing from it were deleted.
            store.variant_links.exclude(variant_id__in=[v["id"] for v in variants]).delete()
            cls._match(store)
        return Counter(store.variant_links.values_list("status", flat=True))

    @classmethod
    def refresh_product(cls, store: ShopifyStore, product_id: str) -> None:
        """Re-read one product (e.g. after products/update) and re-link the store."""
        # An empty result means the product was deleted since the event was sent.
        variants = cls.paginate(
            ShopifyClient(store), cls.PRODUCT_VARIANTS_QUERY, {"id": product_id}, ("product", "variants")
        )
        with transaction.atomic():
            cls._save_variants(store, variants)
            store.variant_links.filter(product_id=product_id).exclude(
                variant_id__in=[v["id"] for v in variants]
            ).delete()
            cls._match(store)

    @staticmethod
    def _save_variants(store: ShopifyStore, variants: list[dict]) -> None:
        now = timezone.now()
        for variant in variants:
            ShopifyVariantLink.objects.update_or_create(
                store=store,
                variant_id=variant["id"],
                defaults={
                    "product_id": variant["product"]["id"],
                    "product_title": variant["product"]["title"][:255],
                    "variant_title": (variant["title"] or "")[:255],
                    "sku": (variant["sku"] or "").strip()[:255],
                    "inventory_item_id": (variant.get("inventoryItem") or {}).get("id", ""),
                    "synced_at": now,
                },
            )

    @classmethod
    def _match(cls, store: ShopifyStore) -> None:
        """Recompute every link's ERP item from SKUs, within the store's company."""
        from inventory.models import InventoryItem

        links = list(store.variant_links.all())
        sku_uses = Counter(cls.sku_key(link.sku) for link in links if cls.sku_key(link.sku))
        items = {
            cls.sku_key(item.code): item
            for item in InventoryItem.objects.filter(company_id=store.company_id).only("id", "code")
            if item.code
        }
        changed = []
        for link in links:
            key = cls.sku_key(link.sku)
            if not key:
                status, item = VariantLinkStatus.NO_SKU, None
            elif sku_uses[key] > 1:
                status, item = VariantLinkStatus.AMBIGUOUS, None
            elif key in items:
                status, item = VariantLinkStatus.MATCHED, items[key]
            else:
                status, item = VariantLinkStatus.NOT_IN_ERP, None
            if link.status != status or link.item_id != (item.pk if item else None):
                link.status, link.item = status, item
                changed.append(link)
        # Clear old items first: two links may swap items within one sync,
        # which the one-item-per-store constraint would reject mid-update.
        if changed:
            ShopifyVariantLink.objects.filter(pk__in=[link.pk for link in changed]).update(item=None)
            ShopifyVariantLink.objects.bulk_update(changed, ["status", "item"])
            # Newly linked variants show Shopify's old number until their stock is pushed.
            ShopifyPushScheduler.schedule_stock_push(link.item_id for link in changed if link.item_id)

    # ── Creating and updating products ───────────────────────────────────

    @classmethod
    def push_catalog(cls, store: ShopifyStore, item_ids: Iterable[int] | None = None) -> CatalogPushResult:
        """Create draft products for the store's items sold online, and keep their price, cost and SKU.

        ``item_ids`` limits it to those items. An item whose SKU already
        exists in Shopify is linked to that product instead of duplicating
        it. Only price, cost and SKU are written after creation: titles,
        descriptions and photos belong to Shopify. Products of items no
        longer sold online are archived, and restored if they are again.

        Where the store's prices include tax, as consumer prices must in
        Spain, the item's tax is added to its ERP (net) price.
        """
        from inventory.models import InventoryItem

        result = CatalogPushResult()
        if not store.push_catalog:
            return result
        lock = cls.PUSH_LOCK.format(store_id=store.pk)
        # Two pushes at once could both create the same new product.
        if not cache.add(lock, True, cls.PUSH_LOCK_SECONDS):
            raise CatalogPushBusy(_("A catalog push for this store is already running. Try again in a moment."))
        try:
            items = InventoryItem.objects.filter(company_id=store.company_id, sell_online=True).order_by("pk")
            if item_ids is not None:
                items = items.filter(pk__in=item_ids)
            links = {
                link.item_id: link
                for link in store.variant_links.filter(status=VariantLinkStatus.MATCHED, item__in=items)
            }
            client = ShopifyClient(store)
            taxes_included = client.graphql(cls.SHOP_TAXES_QUERY)["shop"]["taxesIncluded"]
            tax_rates = items.consumer_tax_rates() if taxes_included else {}
            restored = set()
            for item in items:
                # A long push keeps its lock for as long as it runs.
                cache.touch(lock, cls.PUSH_LOCK_SECONDS)
                try:
                    price = cls._shop_price(item, taxes_included, tax_rates.get(item.pk))
                    sale_mode = store.sale_mode(item)
                    link, created = links.get(item.pk), False
                    if link is None:
                        link, created = cls._link_or_create(client, store, item)
                    elif link.archived_from and link.product_id not in restored:
                        restored.add(link.product_id)
                        if link.archived_from == ProductStatus.ARCHIVED:
                            store.variant_links.filter(product_id=link.product_id).update(archived_from="")
                        else:
                            cls._set_product_status(client, store, link.product_id, link.archived_from)
                            result.restored += 1
                    written = cls._update_variant(client, link, item, price, sale_mode)
                    if created:
                        result.created += 1
                    elif written:
                        result.updated += 1
                    else:
                        result.unchanged += 1
                except ShopifyError as exc:
                    result.errors.append(f"{item.code}: {exc}")
            cls._archive_unsold(client, store, item_ids, result, lock)
        finally:
            cache.delete(lock)
        return result

    @classmethod
    def push_whole_catalog(cls, store: ShopifyStore) -> CatalogPushResult:
        """Push every item sold online and keep the outcome on the store, for its page."""
        try:
            result = cls.push_catalog(store)
        except CatalogPushBusy:
            raise  # the push already running records its own outcome
        except ShopifyError as exc:
            store.record_catalog_push(failure=str(exc))
            raise
        store.record_catalog_push(result)
        return result

    @classmethod
    def _archive_unsold(
        cls,
        client: ShopifyClient,
        store: ShopifyStore,
        item_ids: Iterable[int] | None,
        result: CatalogPushResult,
        lock: str,
    ) -> None:
        """Archive the products of items the ERP pushed and that are no longer sold online.

        A product with other variants still sold online, or variants the ERP
        doesn't manage, stays as it is. The product's status is kept, to be
        restored once the item is sold online again.
        """
        managed = store.variant_links.filter(status=VariantLinkStatus.MATCHED).exclude(pushed_signature="")
        unsold = managed.filter(item__sell_online=False, archived_from="").select_related("item")
        if item_ids is not None:
            unsold = unsold.filter(item_id__in=item_ids)
        for product_id in unsold.values_list("product_id", flat=True).distinct():
            cache.touch(lock, cls.PUSH_LOCK_SECONDS)
            links = list(managed.filter(product_id=product_id).select_related("item"))
            if any(link.item.sell_online for link in links):
                continue
            try:
                product = client.graphql(cls.PRODUCT_STATUS_QUERY, {"id": product_id})["product"]
                if product is None or product["variantsCount"]["count"] > len(links):
                    continue  # gone from Shopify, or sells variants the ERP doesn't manage
                if product["status"] == ProductStatus.ARCHIVED:
                    # Archived in Shopify already: nothing to send, and it stays archived later.
                    managed.filter(product_id=product_id).update(archived_from=ProductStatus.ARCHIVED)
                else:
                    cls._set_product_status(
                        client, store, product_id, ProductStatus.ARCHIVED, archived_from=product["status"]
                    )
                    result.archived += 1
            except ShopifyError as exc:
                result.errors.append(f"{links[0].item.code}: {exc}")

    @classmethod
    def _set_product_status(
        cls, client: ShopifyClient, store: ShopifyStore, product_id: str, status: str, archived_from: str = ""
    ) -> None:
        """Set the product's status in Shopify and remember on its links what to restore."""
        data = client.graphql(cls.PRODUCT_UPDATE, {"product": {"id": product_id, "status": status}})["productUpdate"]
        cls.raise_user_errors(data["userErrors"], _("Shopify refused the product update: %(detail)s"))
        store.variant_links.filter(product_id=product_id).update(archived_from=archived_from)

    @classmethod
    def _link_or_create(
        cls, client: ShopifyClient, store: ShopifyStore, item: InventoryItem
    ) -> tuple[ShopifyVariantLink, bool]:
        """The item's link: an existing Shopify variant with its SKU, or a new draft product."""
        nodes = client.graphql(cls.SKU_QUERY, {"query": f'sku:"{item.code}"'})["productVariants"]["nodes"]
        same_sku = [node for node in nodes if cls.sku_key(node["sku"]) == cls.sku_key(item.code)]
        if len(same_sku) > 1:
            raise ShopifyAPIError(_("The SKU is used by more than one product in Shopify."))
        if same_sku:
            with transaction.atomic():
                cls._save_variants(store, same_sku)
                cls._match(store)
            link = store.variant_links.filter(item=item, status=VariantLinkStatus.MATCHED).first()
            if link is None:
                raise ShopifyAPIError(_("The product with this SKU in Shopify could not be linked."))
            return link, False

        data = client.graphql(cls.PRODUCT_CREATE, {"product": {
            "title": item.name or item.code,
            "descriptionHtml": linebreaks(escape(item.description)) if item.description else "",
            "vendor": store.company.name,
            "status": ProductStatus.DRAFT,
        }})["productCreate"]
        cls.raise_user_errors(data["userErrors"], _("Shopify refused to create the product: %(detail)s"))
        variant = data["product"]["variants"]["nodes"][0]
        # Linked at once, so a failure from here on is retried as an update,
        # never by creating the product a second time.
        link = ShopifyVariantLink.objects.create(
            store=store,
            product_id=data["product"]["id"],
            variant_id=variant["id"],
            inventory_item_id=variant["inventoryItem"]["id"],
            product_title=data["product"]["title"][:255],
            variant_title=(variant["title"] or "")[:255],
            sku=item.code,
            item=item,
            status=VariantLinkStatus.MATCHED,
            synced_at=timezone.now(),
            pushed_signature=cls.NEW_PRODUCT,
        )
        ShopifyPushScheduler.schedule_stock_push([item.pk])
        return link, True

    @classmethod
    def _shop_price(cls, item: InventoryItem, taxes_included: bool, tax_rate: Decimal | None) -> Decimal:
        """The price the store shows: the ERP's net price, plus tax where prices include it."""
        net = item.selling_price.amount if item.selling_price else Decimal("0")
        if not taxes_included:
            return net
        if tax_rate is None:
            raise CatalogItemError(
                _("The store's prices include tax, but the item has no tax. Give it one, or set the company's default taxes.")
            )
        return (net * (1 + tax_rate / 100)).quantize(cls.CENT, rounding=ROUND_HALF_UP)

    @classmethod
    def _update_variant(
        cls, client: ShopifyClient, link: ShopifyVariantLink, item: InventoryItem, price: Decimal, sale_mode: str
    ) -> bool:
        """Send the price, cost, SKU and how it is sold unless Shopify already has them. True when sent."""
        from inventory.choices import ItemType, OnlineSaleMode

        cost = item.cost_price.amount if item.cost_price else None
        fingerprint = f"{item.code}|{price}|{cost}"
        if sale_mode != OnlineSaleMode.BUY:  # bought items keep the fingerprint they always had
            fingerprint += f"|{sale_mode}"
        signature = hashlib.sha256(fingerprint.encode()).hexdigest()
        if link.pushed_signature == signature:
            return False
        inventory = {"sku": item.code}
        if cost is not None:
            inventory["cost"] = str(cost)
        if link.pushed_signature == cls.NEW_PRODUCT:
            # Only for products the ERP created; Shopify owns these settings afterwards.
            ships = item.item_type != ItemType.SERVICE
            inventory.update(tracked=ships and not item.has_components, requiresShipping=ships)
        errors = client.graphql(cls.VARIANT_UPDATE, {
            "productId": link.product_id,
            "variants": [{"id": link.variant_id, "price": str(price), "inventoryItem": inventory}],
        })["productVariantsBulkUpdate"]["userErrors"]
        cls.raise_user_errors(errors, _("Shopify refused the product update: %(detail)s"))
        cls._set_sale_tags(client, link.product_id, sale_mode)
        link.pushed_signature, link.sku = signature, item.code
        link.save(update_fields=["pushed_signature", "sku"])
        return True

    @classmethod
    def _set_sale_tags(cls, client: ShopifyClient, product_id: str, sale_mode: str) -> None:
        """Tag the product for the theme: whether to offer a quote request and whether to hide checkout."""
        from inventory.choices import OnlineSaleMode

        wanted = {
            OnlineSaleMode.BUY: [],
            OnlineSaleMode.QUOTE: [cls.REQUEST_QUOTE_TAG, cls.QUOTE_ONLY_TAG],
            OnlineSaleMode.BOTH: [cls.REQUEST_QUOTE_TAG],
        }[sale_mode]
        unwanted = [tag for tag in (cls.REQUEST_QUOTE_TAG, cls.QUOTE_ONLY_TAG) if tag not in wanted]
        for mutation, tags, payload in ((cls.TAGS_REMOVE, unwanted, "tagsRemove"), (cls.TAGS_ADD, wanted, "tagsAdd")):
            if not tags:
                continue
            errors = client.graphql(mutation, {"id": product_id, "tags": tags})[payload]["userErrors"]
            cls.raise_user_errors(errors, _("Shopify refused the product update: %(detail)s"))
