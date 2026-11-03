"""
Linking a store's Shopify locations and product variants to ERP warehouses and
items, and creating products in Shopify for items sold online.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.html import escape, linebreaks
from django.utils.translation import gettext as _

from apps.shopify.client import ShopifyClient
from apps.shopify.enums import InventoryPolicy, ProductStatus
from apps.shopify.exceptions import ShopifyAPIError, ShopifyBusy, ShopifyError
from apps.shopify.models import ShopifyLocationMap, ShopifyStore, ShopifyVariantLink, VariantLinkStatus
from apps.shopify.services.base import ShopifyService
from apps.shopify.services.product_fields import ProductField, ShopifyProductFields
from apps.shopify.services.push_scheduler import ShopifyPushScheduler
from apps.shopify.services.stock import ShopifyStockService

if TYPE_CHECKING:
    from inventory.models import InventoryItem


class CatalogPushBusy(ShopifyBusy):
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
    removed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VariantOptions:
    """Where an item sits among the variants of a grouped product."""

    group: tuple  # the configurator template and its option names: the product the item belongs to
    title: str  # the product's title and description, should it have to be created
    description: str
    names: tuple[str, ...]
    values: tuple[str, ...]
    position: tuple[int, ...]  # the values' places in the template's option lists, for ordering


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
    PRODUCT_CREATE = f"""
    mutation ($product: ProductCreateInput!) {{
      productCreate(product: $product) {{
        product {{ id title variants(first: 1) {{ nodes {{ {VARIANT_FIELDS} }} }} }}
        userErrors {{ field message }}
      }}
    }}
    """
    VARIANTS_CREATE = f"""
    mutation ($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {{
      productVariantsBulkCreate(productId: $productId, variants: $variants) {{
        productVariants {{ {VARIANT_FIELDS} }}
        userErrors {{ field message }}
      }}
    }}
    """
    VARIANTS_DELETE = """
    mutation ($productId: ID!, $variantsIds: [ID!]!) {
      productVariantsBulkDelete(productId: $productId, variantsIds: $variantsIds) { userErrors { field message } }
    }
    """
    VARIANT_UPDATE = """
    mutation ($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkUpdate(productId: $productId, variants: $variants) {
        userErrors { field message }
      }
    }
    """
    STOCK_SETTINGS_QUERY = """
    query ($id: ID!) { productVariant(id: $id) { inventoryPolicy inventoryItem { tracked } } }
    """
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
    MAX_PRODUCT_OPTIONS = 3  # Shopify's limit

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
        it. Only price, cost and SKU are written after creation, and the
        product type when the store takes it from a category field: titles,
        descriptions and photos belong to Shopify. Products of items no
        longer sold online are archived, and restored if they are again.
        Where the store groups configurator items, an item joins its group's
        product as a variant, and leaves it when no longer sold online.

        Where the store's prices include tax, as consumer prices must in
        Spain, the item's tax is added to its ERP (net) price.
        """
        from inventory.models import InventoryItem

        result = CatalogPushResult()
        if not store.push_catalog:
            return result
        if item_ids is not None:
            item_ids = set(item_ids)
        # Two pushes at once could both create the same new product.
        busy = CatalogPushBusy(_("A catalog push for this store is already running. Try again in a moment."))
        with cls.store_lock(cls.PUSH_LOCK.format(store_id=store.pk), cls.PUSH_LOCK_SECONDS, busy) as keep_lock:
            items = (
                InventoryItem.objects.filter(company_id=store.company_id, sell_online=True)
                .with_configuration_preset()
                # The product fields read every item's schema: loaded once, not per item.
                .select_related("metadata_schema")
                .prefetch_related("metadata_schema__fields", "metadata_schema__includes__fields")
                .order_by("pk")
            )
            if item_ids is not None:
                items = items.filter(pk__in=item_ids)
            links = {
                link.item_id: link
                for link in store.variant_links.filter(status=VariantLinkStatus.MATCHED, item__in=items)
            }
            options = cls._variant_options(store)
            group_products = cls._group_products(store, options)
            client = ShopifyClient(store)
            taxes_included = cls.taxes_included(client)
            tax_rates = items.consumer_tax_rates() if taxes_included else {}
            restored, touched = set(), set()
            # A group's new product takes its options' values in the order its first variants bring them.
            ordered = sorted(items, key=lambda item: (0, (), ()) if item.pk not in options else (
                1, options[item.pk].group, options[item.pk].position
            ))
            for item in ordered:
                # A long push keeps its lock for as long as it runs.
                keep_lock()
                try:
                    price = cls._shop_price(item, taxes_included, tax_rates.get(item.pk))
                    sale_mode = store.sale_mode(item)
                    link, created = links.get(item.pk), False
                    if link is None:
                        link, created = cls._link_or_create(client, store, item, options.get(item.pk), group_products)
                    elif link.archived_from and link.product_id not in restored:
                        restored.add(link.product_id)
                        if link.archived_from == ProductStatus.ARCHIVED:
                            store.variant_links.filter(product_id=link.product_id).update(archived_from="")
                        else:
                            cls._set_product_status(client, store, link.product_id, link.archived_from)
                            result.restored += 1
                    written = cls._update_variant(client, link, item, price, sale_mode, store.product_type(item))
                    written = cls._write_fields(client, link, ShopifyProductFields.for_item(store, item)) or written
                    if written:
                        touched.add(link.product_id)
                    if created:
                        result.created += 1
                    elif written:
                        result.updated += 1
                    else:
                        result.unchanged += 1
                except ShopifyError as exc:
                    result.errors.append(f"{item.code}: {exc}")
            modes = cls._sale_modes(store, touched)
            for product_id in touched:
                keep_lock()
                try:
                    cls._set_sale_tags(client, product_id, modes.get(product_id, set()))
                except ShopifyError as exc:
                    result.errors.append(f"{product_id}: {exc}")
            cls._archive_unsold(client, store, item_ids, result, keep_lock)
        return result

    @classmethod
    def _variant_options(cls, store: ShopifyStore) -> dict[int, VariantOptions]:
        """The variant each grouped item is, keyed by item id; empty unless the store groups variants.

        Items sold online and made from a preset that fixes every required
        parameter of its configurator template, at most three, are grouped by
        template. Built from all of them, whatever the push covers, so a
        product's title and options don't depend on which items are pushed.
        """
        from configurator.models import ConfigurationPreset
        from inventory.models import InventoryItem

        if not store.group_variants:
            return {}
        items = InventoryItem.objects.filter(
            company_id=store.company_id,
            sell_online=True,
            product_content_type=ContentType.objects.get_for_model(ConfigurationPreset),
        ).order_by("pk").values_list("pk", "name", "code", "product_object_id")
        presets = ConfigurationPreset.objects.filter(
            company_id=store.company_id, pk__in={preset_id for *_, preset_id in items}
        ).select_related("product").prefetch_related("product__parameters").in_bulk()

        members = {}
        for item_id, name, code, preset_id in items:
            preset = presets.get(preset_id)
            if preset is None or preset.get_missing_parameters():
                continue  # made to measure: the customer chooses its values
            chosen = preset.chosen_parameters()
            if not chosen or len(chosen) > cls.MAX_PRODUCT_OPTIONS:
                continue
            names = tuple(parameter.label or parameter.name for parameter, _value in chosen)
            position = tuple(cls._option_position(parameter.options, value) for parameter, value in chosen)
            members.setdefault((preset.product_id, names), []).append(
                (item_id, name or code, tuple(value for _parameter, value in chosen), position, preset.product)
            )

        options = {}
        for group, entries in members.items():
            template = entries[0][4]
            title = cls._group_title([name for _item_id, name, *_ in entries]) or template.name
            for item_id, _name, values, position, _template in entries:
                options[item_id] = VariantOptions(group, title, template.description, group[1], values, position)
        return options

    @staticmethod
    def _option_position(options: list | None, value: str) -> int:
        """Where the value comes in the parameter's options; values it doesn't offer go last."""
        offered = [str(option) for option in options or []]
        return offered.index(value) if value in offered else len(offered)

    @staticmethod
    def _group_title(names: list[str]) -> str:
        """The words every name shares, in the first name's order: "Tank Mod. 160 (Blue)" and "Tank Mod. 500 (Red)" give "Tank Mod."."""
        shared = set(names[0].split()).intersection(*(name.split() for name in names[1:]))
        return " ".join(word for word in names[0].split() if word in shared)

    @staticmethod
    def _group_products(store: ShopifyStore, options: dict[int, VariantOptions]) -> dict[tuple, str]:
        """The Shopify product each group already has, from its linked items."""
        products = {}
        for item_id, product_id in store.variant_links.filter(
            status=VariantLinkStatus.MATCHED, item_id__in=options
        ).values_list("item_id", "product_id"):
            products.setdefault(options[item_id].group, product_id)
        return products

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
        keep_lock: Callable[[], None],
    ) -> None:
        """Take items the ERP pushed and that are no longer sold online off the store.

        A product with other variants still sold online loses the variants the
        ERP created for those items; one with none left is archived, its status
        kept to be restored once an item is sold online again. A variant only
        linked by its SKU is never deleted, and a product with variants the ERP
        doesn't manage stays as it is.
        """
        managed = store.variant_links.filter(status=VariantLinkStatus.MATCHED).exclude(pushed_signature="")
        unsold = managed.filter(item__sell_online=False, archived_from="").select_related("item")
        if item_ids is not None:
            unsold = unsold.filter(item_id__in=item_ids)
        for product_id in unsold.values_list("product_id", flat=True).distinct():
            keep_lock()
            links = list(managed.filter(product_id=product_id).select_related("item"))
            if any(link.item.sell_online for link in links):
                cls._remove_variants(client, store, product_id, [
                    link
                    for link in links
                    if link.created_by_erp and not link.item.sell_online and (item_ids is None or link.item_id in item_ids)
                ], result)
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
    def _remove_variants(
        cls, client: ShopifyClient, store: ShopifyStore, product_id: str, links: list[ShopifyVariantLink],
        result: CatalogPushResult,
    ) -> None:
        """Delete these variants from a product that keeps others, and forget their links."""
        if not links:
            return
        try:
            data = client.graphql(cls.VARIANTS_DELETE, {
                "productId": product_id, "variantsIds": [link.variant_id for link in links],
            })["productVariantsBulkDelete"]
            cls.raise_user_errors(data["userErrors"], _("Shopify refused to remove the variant: %(detail)s"))
        except ShopifyError as exc:
            result.errors.append(f"{links[0].item.code}: {exc}")
            return
        store.variant_links.filter(pk__in=[link.pk for link in links]).delete()
        result.removed += len(links)

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
        cls,
        client: ShopifyClient,
        store: ShopifyStore,
        item: InventoryItem,
        options: VariantOptions | None,
        group_products: dict[tuple, str],
    ) -> tuple[ShopifyVariantLink, bool]:
        """The item's link: an existing Shopify variant with its SKU, else a new variant or draft product.

        A grouped item becomes a variant of its group's product, which the
        group's first item creates with the item's option values.
        """
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

        if options is not None and options.group in group_products:
            data = client.graphql(cls.VARIANTS_CREATE, {
                "productId": group_products[options.group],
                "variants": [{"optionValues": [
                    {"optionName": name, "name": value} for name, value in zip(options.names, options.values)
                ]}],
            })["productVariantsBulkCreate"]
            cls.raise_user_errors(data["userErrors"], _("Shopify refused to add the variant: %(detail)s"))
            return cls._link_new_variant(store, item, data["productVariants"][0]), True

        # A grouped product describes its template, never one of its items.
        description = options.description if options else item.description
        product = {
            "title": options.title if options else item.name or item.code,
            "descriptionHtml": linebreaks(escape(description)) if description else "",
            "vendor": store.company.name,
            "status": ProductStatus.DRAFT,
        }
        if options is not None:
            product["productOptions"] = [
                {"name": name, "values": [{"name": value}]} for name, value in zip(options.names, options.values)
            ]
        data = client.graphql(cls.PRODUCT_CREATE, {"product": product})["productCreate"]
        cls.raise_user_errors(data["userErrors"], _("Shopify refused to create the product: %(detail)s"))
        if options is not None:
            group_products[options.group] = data["product"]["id"]
        return cls._link_new_variant(store, item, data["product"]["variants"]["nodes"][0]), True

    @classmethod
    def _link_new_variant(cls, store: ShopifyStore, item: InventoryItem, variant: dict) -> ShopifyVariantLink:
        """Link a variant the ERP just created.

        Linked at once, so a failure from here on is retried as an update,
        never by creating the variant a second time.
        """
        link = ShopifyVariantLink.objects.create(
            store=store,
            product_id=variant["product"]["id"],
            variant_id=variant["id"],
            inventory_item_id=variant["inventoryItem"]["id"],
            product_title=variant["product"]["title"][:255],
            variant_title=(variant["title"] or "")[:255],
            sku=item.code,
            item=item,
            status=VariantLinkStatus.MATCHED,
            synced_at=timezone.now(),
            pushed_signature=cls.NEW_PRODUCT,
            created_by_erp=True,
        )
        ShopifyPushScheduler.schedule_stock_push([item.pk])
        return link

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
        cls,
        client: ShopifyClient,
        link: ShopifyVariantLink,
        item: InventoryItem,
        price: Decimal,
        sale_mode: str,
        product_type: str | None,
    ) -> bool:
        """Send the price, cost, SKU, how it is sold and its product type unless Shopify already has them.

        ``product_type`` None leaves the product type to Shopify. A variant
        that is only quoted can't be bought in Shopify either, whatever the
        theme shows: its stock is tracked at zero and never oversold. What
        that changed is kept on the link and restored once it is sold again.
        True when sent.
        """
        from inventory.choices import ItemType, OnlineSaleMode

        cost = item.cost_price.amount if item.cost_price else None
        fingerprint = f"{item.code}|{price}|{cost}"
        # Additions only when they apply, so existing products keep the fingerprint they always had.
        if sale_mode != OnlineSaleMode.BUY:
            fingerprint += f"|{sale_mode}"
        if sale_mode == OnlineSaleMode.QUOTE:
            fingerprint += "|unsellable"  # variants quoted before Shopify was made to refuse them are sent again
        if product_type is not None:
            fingerprint += f"|type={product_type}"
        signature = hashlib.sha256(fingerprint.encode()).hexdigest()
        if link.pushed_signature == signature:
            return False
        inventory = {"sku": item.code}
        if cost is not None:
            inventory["cost"] = str(cost)
        variant = {"id": link.variant_id, "price": str(price), "inventoryItem": inventory}
        new_settings = None
        if link.pushed_signature == cls.NEW_PRODUCT:
            # Only for products the ERP created; Shopify owns these settings afterwards.
            ships = item.item_type != ItemType.SERVICE
            inventory.update(tracked=ships and not item.has_components, requiresShipping=ships)
            new_settings = {"tracked": inventory["tracked"], "inventoryPolicy": InventoryPolicy.DENY}
        quote_only = sale_mode == OnlineSaleMode.QUOTE
        if quote_only:
            if link.quote_only_restore is None:
                # Kept first, so a failure further on never records the ERP's own settings.
                link.quote_only_restore = new_settings or cls._stock_settings(client, link)
                link.save(update_fields=["quote_only_restore"])
            inventory["tracked"] = True
            variant["inventoryPolicy"] = InventoryPolicy.DENY
        elif link.quote_only_restore is not None:
            inventory["tracked"] = link.quote_only_restore["tracked"]
            variant["inventoryPolicy"] = link.quote_only_restore["inventoryPolicy"]
        errors = client.graphql(cls.VARIANT_UPDATE, {
            "productId": link.product_id, "variants": [variant],
        })["productVariantsBulkUpdate"]["userErrors"]
        cls.raise_user_errors(errors, _("Shopify refused the product update: %(detail)s"))
        if quote_only:
            ShopifyStockService.clear(client, link.store, link)
        elif link.quote_only_restore is not None:
            link.quote_only_restore = None
            # Sold again: Shopify gets the real stock instead of the zero it was held at.
            ShopifyPushScheduler.schedule_stock_push([item.pk])
        if product_type is not None:
            data = client.graphql(cls.PRODUCT_UPDATE, {"product": {"id": link.product_id, "productType": product_type}})
            cls.raise_user_errors(data["productUpdate"]["userErrors"], _("Shopify refused the product update: %(detail)s"))
        link.pushed_signature, link.sku = signature, item.code
        link.save(update_fields=["pushed_signature", "sku", "quote_only_restore"])
        return True

    @classmethod
    def _stock_settings(cls, client: ShopifyClient, link: ShopifyVariantLink) -> dict:
        """How Shopify handles the variant's stock now: {"tracked", "inventoryPolicy"}."""
        variant = client.graphql(cls.STOCK_SETTINGS_QUERY, {"id": link.variant_id})["productVariant"]
        return {"tracked": variant["inventoryItem"]["tracked"], "inventoryPolicy": variant["inventoryPolicy"]}

    @classmethod
    def _write_fields(cls, client: ShopifyClient, link: ShopifyVariantLink, fields: list[ProductField]) -> bool:
        """Write the item's product fields on its variant unless Shopify already has them. True when sent.

        Fields without a value are deleted, which only a variant that had
        fields can need.
        """
        signature = ShopifyProductFields.fingerprint(fields)
        if signature == link.fields_signature:
            return False
        message = _("Shopify refused the product update: %(detail)s")
        cls.set_metafields(client, [
            {"ownerId": link.variant_id, "key": field.key, "type": ShopifyProductFields.METAFIELD_TYPE, "value": field.json}
            for field in fields
            if field.value is not None
        ], message)
        if link.fields_signature:
            cls.delete_metafields(
                client, [{"ownerId": link.variant_id, "key": field.key} for field in fields if field.value is None], message
            )
        link.fields_signature = signature
        link.save(update_fields=["fields_signature"])
        return True

    @staticmethod
    def _sale_modes(store: ShopifyStore, product_ids: Iterable[str]) -> dict[str, set[str]]:
        """How the variants of these products sell, per product: their items sold online, in one query."""
        from inventory.models import InventoryItem

        modes: dict[str, set[str]] = {}
        for link in store.variant_links.filter(
            product_id__in=product_ids, status=VariantLinkStatus.MATCHED, item__sell_online=True
        ).prefetch_related(Prefetch("item", queryset=InventoryItem.objects.with_configuration_preset())):
            modes.setdefault(link.product_id, set()).add(store.sale_mode(link.item))
        return modes

    @classmethod
    def _set_sale_tags(cls, client: ShopifyClient, product_id: str, modes: set[str]) -> None:
        """Tag the product for the theme by how its variants sell (``modes``, from ``_sale_modes``).

        "request-quote" when any of them can be quoted, "quote-only" when none
        can be bought. Each variant's own way is its sale_mode field.
        """
        from inventory.choices import OnlineSaleMode

        wanted = []
        if modes & {OnlineSaleMode.QUOTE, OnlineSaleMode.BOTH}:
            wanted.append(cls.REQUEST_QUOTE_TAG)
        if modes == {OnlineSaleMode.QUOTE}:
            wanted.append(cls.QUOTE_ONLY_TAG)
        unwanted = [tag for tag in (cls.REQUEST_QUOTE_TAG, cls.QUOTE_ONLY_TAG) if tag not in wanted]
        for mutation, tags, payload in ((cls.TAGS_REMOVE, unwanted, "tagsRemove"), (cls.TAGS_ADD, wanted, "tagsAdd")):
            if not tags:
                continue
            errors = client.graphql(mutation, {"id": product_id, "tags": tags})[payload]["userErrors"]
            cls.raise_user_errors(errors, _("Shopify refused the product update: %(detail)s"))
