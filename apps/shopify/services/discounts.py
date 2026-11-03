"""
Offering clients' discounts in a store, only to the customers staff gave
web store access.

``ChannelDiscountService`` translates each client's discount policies.
Clients whose policies come to the same discounts share a
``ShopifyDiscountProfile``: a product discount and one order discount per
volume tier, each a Shopify code discount with one secret code and open only
to the profile's customers. A customer gets the profile's codes in a
metafield, which the theme applies once they sign in. A guest who types a
client's email gets nothing, as every discount needs its code.

Shopify does the arithmetic as the quote's cascade does: the product
discount first, then the best tier whose minimum the discounted subtotal
reaches. Thresholds are net amounts; a store whose prices include tax gets
them with the company's default tax added.

The ERP records what it published (profiles, and each customer's profile),
so a run only sends what changed. Changes made by hand in Shopify to these
discounts are not repaired.

Customers are given web store access by staff (``give_web_store_access``): the
client's Shopify customer is found by its email, or created, and can then
sign in with that email. Nobody links themselves to a client.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.shopify.client import ShopifyClient
from apps.shopify.enums import AccessScope
from apps.shopify.exceptions import ShopifyAuthError, ShopifyBusy, ShopifyError
from apps.shopify.models import ShopifyCustomerLink, ShopifyDiscountProfile, ShopifyStore
from apps.shopify.services.base import ShopifyService
from apps.shopify.services.push_scheduler import ShopifyPushScheduler
from company.models import CompanyDefaultTax
from sales.models import Client
from sales.services.channel_discounts import ChannelDiscount, ChannelDiscountService

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class ClientAccessError(ShopifyError):
    """The client can't be given web store access as it is; the message says what to fix."""


@dataclass(frozen=True)
class DiscountOffer:
    """A client's discounts as the store publishes them."""

    product_percentage: Decimal
    tiers: tuple[tuple[Decimal, Decimal], ...] = ()  # (order subtotal Shopify compares, percentage)

    @property
    def signature(self) -> str:
        """What identifies the offer: equal offers share one profile."""
        return " · ".join(
            [f"{self.product_percentage}%", *(f"{percentage}% ≥ {minimum}" for minimum, percentage in self.tiers)]
        )

    @property
    def discount_count(self) -> int:
        return (1 if self.product_percentage else 0) + len(self.tiers)


@dataclass
class ClientAccessResult:
    given: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class DiscountSyncResult:
    offered: int = 0  # customers with client prices in Shopify after the run
    changed: int = 0  # customers whose discounts changed in this run
    errors: list[str] = field(default_factory=list)


class ShopifyDiscountService(ShopifyService):
    REQUIRED_SCOPES = (AccessScope.WRITE_DISCOUNTS, AccessScope.WRITE_CUSTOMERS)
    # Customer metafields the theme reads to apply the codes and show prices.
    CODES_KEY = "client_discount_codes"
    PRODUCT_PERCENTAGE_KEY = "client_product_discount"
    CODE_ALPHABET = string.ascii_uppercase + string.digits
    CODE_LENGTH = 16
    CUSTOMER_BATCH = 100
    SYNC_LOCK = "shopify:discount-sync-lock:{store_id}"
    SYNC_LOCK_SECONDS = 900

    COLLECTION_QUERY = "query ($id: ID!) { collection(id: $id) { id } }"
    COLLECTION_CREATE = """
    mutation ($input: CollectionInput!) {
      collectionCreate(input: $input) { collection { id } userErrors { field message } }
    }
    """
    DISCOUNT_CREATE = """
    mutation ($discount: DiscountCodeBasicInput!) {
      discountCodeBasicCreate(basicCodeDiscount: $discount) { codeDiscountNode { id } userErrors { field message } }
    }
    """
    DISCOUNT_UPDATE = """
    mutation ($id: ID!, $discount: DiscountCodeBasicInput!) {
      discountCodeBasicUpdate(id: $id, basicCodeDiscount: $discount) { codeDiscountNode { id } userErrors { field message } }
    }
    """
    DISCOUNT_DELETE = """
    mutation ($id: ID!) { discountCodeDelete(id: $id) { deletedCodeDiscountId userErrors { field message } } }
    """
    DISCOUNT_QUERY = "query ($id: ID!) { codeDiscountNode(id: $id) { id } }"
    CUSTOMER_QUERY = "query ($query: String!) { customers(first: 5, query: $query) { nodes { id email } } }"
    CUSTOMER_CREATE = """
    mutation ($input: CustomerInput!) { customerCreate(input: $input) { customer { id } userErrors { field message } } }
    """

    # ── Who gets web store access ────────────────────────────────────────

    @classmethod
    def give_web_store_access(cls, store: ShopifyStore, clients: Iterable[Client]) -> ClientAccessResult:
        """Give these clients' Shopify customers their client's discounts, creating the customers as needed.

        The customer is the one with the client's email, so only whoever reads
        that inbox can sign in. The discounts follow in the next sync.
        """
        if not store.has_scope(AccessScope.WRITE_CUSTOMERS):
            raise ShopifyAuthError(_("Connect the store to Shopify again to allow client discounts."))
        shopify = ShopifyClient(store)
        result, link_ids = ClientAccessResult(), []
        for client in clients:
            try:
                link_ids.append(cls._customer_link(shopify, store, client).pk)
            except ShopifyError as exc:
                result.errors.append(f"{client}: {exc}")
                continue
            result.given.append(str(client))
        cls.set_web_store_access(ShopifyCustomerLink.objects.filter(pk__in=link_ids), True)
        return result

    @staticmethod
    def set_web_store_access(links: QuerySet[ShopifyCustomerLink], enabled: bool) -> int:
        """Give or take away web store access for these customers; the next sync publishes it. Returns how many changed."""
        changed = links.exclude(web_store_access=enabled)
        company_ids = set(changed.values_list("store__company_id", flat=True))
        count = changed.update(web_store_access=enabled)
        ShopifyPushScheduler.schedule_discount_sync(company_ids)
        return count

    @classmethod
    def _customer_link(cls, shopify: ShopifyClient, store: ShopifyStore, client: Client) -> ShopifyCustomerLink:
        """The link of the client's Shopify customer: the one with its email, created if there is none."""
        if client.company_id != store.company_id:
            raise ClientAccessError(_("The client belongs to another company."))
        email = (client.email or "").strip()
        if not email:
            raise ClientAccessError(_("The client has no email to sign in with."))
        if Client.objects.filter(company_id=client.company_id, email__iexact=email).exclude(pk=client.pk).exists():
            raise ClientAccessError(_("Another client has the same email, and Shopify allows one customer per email."))
        search = 'email:"%s"' % email.replace('"', "")
        nodes = shopify.graphql(cls.CUSTOMER_QUERY, {"query": search})["customers"]["nodes"]
        customer_id = next((node["id"] for node in nodes if (node["email"] or "").lower() == email.lower()), None)
        if customer_id is None:
            data = shopify.graphql(cls.CUSTOMER_CREATE, {"input": {"email": email, "firstName": client.name[:255]}})
            cls.raise_user_errors(
                data["customerCreate"]["userErrors"], _("Shopify refused to create the customer: %(detail)s")
            )
            customer_id = data["customerCreate"]["customer"]["id"]
        link, _created = ShopifyCustomerLink.objects.get_or_create(
            store=store, customer_id=customer_id, defaults={"client": client}
        )
        if link.client_id != client.pk:
            raise ClientAccessError(
                _("The Shopify customer with this email belongs to %(client)s.") % {"client": link.client}
            )
        return link

    @classmethod
    def sync(cls, store: ShopifyStore) -> DiscountSyncResult:
        """Bring the store's client discounts in line with the ERP's policies and customers."""
        if not all(store.has_scope(scope) for scope in cls.REQUIRED_SCOPES):
            raise ShopifyAuthError(_("Connect the store to Shopify again to allow client discounts."))
        busy = ShopifyBusy(_("This store's client discounts are already being updated. Try again in a moment."))
        with cls.store_lock(cls.SYNC_LOCK.format(store_id=store.pk), cls.SYNC_LOCK_SECONDS, busy) as keep_lock:
            client = ShopifyClient(store)
            result = DiscountSyncResult()
            links = list(store.customer_links.select_related("client").order_by("pk"))
            offers, errors = cls._offers(store, client, links)
            published = cls._publish(client, store, links, offers, keep_lock)
            cls._update_customers(client, links, published, errors, result)
            return result

    # ── What each customer should get ────────────────────────────────────

    @classmethod
    def _offers(
        cls, store: ShopifyStore, client: ShopifyClient, links: list[ShopifyCustomerLink]
    ) -> tuple[dict[int, DiscountOffer], dict[int, str]]:
        """Per link id, the customer's offer (none when they get nothing), and the reasons some get nothing."""
        offering = store.is_active and store.sync_client_discounts
        wanted = [link for link in links if offering and link.web_store_access]
        taxes_included = cls.taxes_included(client) if wanted else False
        tax_rate = CompanyDefaultTax.consumer_rates([store.company_id]).get(store.company_id)
        offers, errors = {}, {}
        for link in wanted:
            discount = ChannelDiscountService.for_client(store.company, link.client)
            if not discount.is_supported:
                errors[link.pk] = discount.unsupported
            elif not discount.is_empty:
                offer = cls._offer(discount, taxes_included, tax_rate)
                if offer is None:
                    errors[link.pk] = _(
                        "The store's prices include tax, but the company has no default taxes to add to the "
                        "volume discount thresholds."
                    )
                else:
                    offers[link.pk] = offer
        return offers, errors

    @classmethod
    def _offer(cls, discount: ChannelDiscount, taxes_included: bool, tax_rate: Decimal | None) -> DiscountOffer | None:
        """The discount in Shopify's terms: thresholds with tax where the store's prices include it."""
        tiers = []
        for tier in discount.tiers:
            minimum = tier.threshold
            if taxes_included and minimum:
                if tax_rate is None:
                    return None
                minimum = (minimum * (1 + tax_rate / 100)).quantize(cls.CENT, rounding=ROUND_HALF_UP)
            tiers.append((minimum.quantize(cls.CENT), tier.percentage))
        return DiscountOffer(discount.product_percentage, tuple(tiers))

    # ── Shopify discounts ────────────────────────────────────────────────

    @classmethod
    def _publish(
        cls,
        client: ShopifyClient,
        store: ShopifyStore,
        links: list[ShopifyCustomerLink],
        offers: dict[int, DiscountOffer],
        keep_lock: Callable[[], None],
    ) -> dict[int, ShopifyDiscountProfile]:
        """Create, update and remove the store's profiles. Returns each offered link's profile.

        A link records the profile whose discounts it is open to as soon as
        Shopify confirms it, so an interrupted run never leaves a customer
        eligible without the ERP knowing; the next run carries on from there.
        """
        customers = {link.pk: link.customer_id for link in links}
        eligible: dict[int, set[str]] = {}  # what Shopify has, read once: the order profiles run in can't matter
        for link in links:
            if link.discount_profile_id:
                eligible.setdefault(link.discount_profile_id, set()).add(link.customer_id)
        wanted: dict[str, list[int]] = {}
        for link_id, offer in offers.items():
            wanted.setdefault(offer.signature, []).append(link_id)
        by_signature = {offer.signature: offer for offer in offers.values()}
        collection_id = cls._collection(client, store) if any(o.product_percentage for o in offers.values()) else ""

        profiles = {profile.signature: profile for profile in store.discount_profiles.all()}
        published = {}
        for signature, link_ids in wanted.items():
            keep_lock()
            offer, profile = by_signature[signature], profiles.pop(signature, None)
            if profile is not None and not cls._is_complete(profile, offer, collection_id):
                cls._retire(client, profile)  # a creation that failed half-way, or an old collection
                profile = None
            ids = {customers[link_id] for link_id in link_ids}
            removed = set()
            if profile is None:
                profile = cls._create_profile(client, store, offer, sorted(ids), collection_id)
            else:
                current = eligible.get(profile.pk, set())
                removed = current - ids
                cls._set_customers(client, profile, sorted(ids - current), sorted(removed))
            store.customer_links.filter(pk__in=link_ids).update(discount_profile=profile)
            store.customer_links.filter(customer_id__in=removed, discount_profile=profile).update(discount_profile=None)
            published.update(dict.fromkeys(link_ids, profile))
        for profile in profiles.values():  # no customer gets these any more
            keep_lock()
            cls._retire(client, profile)
        return published

    @classmethod
    def _collection(cls, client: ShopifyClient, store: ShopifyStore) -> str:
        """The store's automated collection of every priced product, created if it is missing."""
        if store.discount_collection_id:
            if client.graphql(cls.COLLECTION_QUERY, {"id": store.discount_collection_id})["collection"]:
                return store.discount_collection_id
        data = client.graphql(cls.COLLECTION_CREATE, {"input": {
            "title": cls.erp_text(gettext_noop("All products (client discounts)")),
            "descriptionHtml": cls.erp_text(gettext_noop("Kept by the ERP for client discounts. Don't edit or delete it.")),
            "ruleSet": {
                "appliedDisjunctively": False,
                "rules": [{"column": "VARIANT_PRICE", "relation": "GREATER_THAN", "condition": "0"}],
            },
        }})["collectionCreate"]
        cls.raise_user_errors(data["userErrors"], _("Shopify refused to create the collection: %(detail)s"))
        store.discount_collection_id = data["collection"]["id"]
        # Not save(): a store save schedules a discount sync.
        ShopifyStore.objects.filter(pk=store.pk).update(discount_collection_id=store.discount_collection_id)
        return store.discount_collection_id

    @staticmethod
    def _is_complete(profile: ShopifyDiscountProfile, offer: DiscountOffer, collection_id: str) -> bool:
        if len(profile.discounts) != offer.discount_count:
            return False
        return all(d.get("collection_id") == collection_id for d in profile.discounts if d["minimum"] is None)

    @classmethod
    def _create_profile(
        cls,
        client: ShopifyClient,
        store: ShopifyStore,
        offer: DiscountOffer,
        customer_ids: list[str],
        collection_id: str,
    ) -> ShopifyDiscountProfile:
        """The offer's discounts in Shopify, open to ``customer_ids``.

        Each discount is recorded as soon as it exists, so a failure half-way
        leaves nothing unknown behind: the next run replaces the profile.
        """
        profile = ShopifyDiscountProfile.objects.create(store=store, signature=offer.signature)
        first, rest = customer_ids[:cls.CUSTOMER_BATCH], customer_ids[cls.CUSTOMER_BATCH:]
        parts = [(offer.product_percentage, None)] if offer.product_percentage else []
        parts += [(percentage, minimum) for minimum, percentage in offer.tiers]
        for percentage, minimum in parts:
            code = "".join(secrets.choice(cls.CODE_ALPHABET) for _position in range(cls.CODE_LENGTH))
            discount = cls._discount_input(code, percentage, minimum, collection_id)
            discount["context"] = {"customers": {"add": first}}
            data = client.graphql(cls.DISCOUNT_CREATE, {"discount": discount})["discountCodeBasicCreate"]
            cls.raise_user_errors(data["userErrors"], _("Shopify refused to create the discount: %(detail)s"))
            entry = {
                "discount_id": data["codeDiscountNode"]["id"],
                "code": code,
                "percentage": str(percentage),
                "minimum": None if minimum is None else str(minimum),
            }
            if minimum is None:
                entry["collection_id"] = collection_id
            profile.discounts.append(entry)
            profile.save(update_fields=["discounts"])
        cls._set_customers(client, profile, rest, [])
        profile.synced_at = timezone.now()
        profile.save(update_fields=["synced_at"])
        return profile

    @staticmethod
    def _discount_input(code: str, percentage: Decimal, minimum: Decimal | None, collection_id: str) -> dict:
        """A code discount: the product discount when ``minimum`` is None, else a volume tier.

        The product discount combines with order discounts and a tier with
        product discounts, so Shopify applies one product discount and then
        the best tier, as the quote does. Tiers never combine with each other.
        """
        shown = f"{percentage.normalize():f}"
        discount = {
            "code": code,
            "startsAt": timezone.now().isoformat(),
            "appliesOncePerCustomer": False,
            "usageLimit": None,
            # Shopify takes a fraction of one here; nothing is computed with it.
            "customerGets": {"value": {"percentage": float(percentage / 100)}},
        }
        if minimum is None:
            discount["title"] = ShopifyService.erp_text(gettext_noop("Client discount %(percentage)s%%"), percentage=shown)
            discount["customerGets"]["items"] = {"collections": {"add": [collection_id]}}
            discount["combinesWith"] = {"productDiscounts": False, "orderDiscounts": True, "shippingDiscounts": True}
        else:
            discount["title"] = ShopifyService.erp_text(gettext_noop("Volume discount %(percentage)s%%"), percentage=shown)
            discount["customerGets"]["items"] = {"all": True}
            discount["combinesWith"] = {"productDiscounts": True, "orderDiscounts": False, "shippingDiscounts": True}
            if minimum:
                discount["minimumRequirement"] = {"subtotal": {"greaterThanOrEqualToSubtotal": str(minimum)}}
        return discount

    @classmethod
    def _set_customers(
        cls, client: ShopifyClient, profile: ShopifyDiscountProfile, add: list[str], remove: list[str]
    ) -> None:
        """Open the profile's discounts to the ``add`` customers and close them to the ``remove`` ones."""
        for start in range(0, max(len(add), len(remove)), cls.CUSTOMER_BATCH):
            customers = {
                key: ids[start:start + cls.CUSTOMER_BATCH]
                for key, ids in (("add", add), ("remove", remove))
                if ids[start:start + cls.CUSTOMER_BATCH]
            }
            for discount in profile.discounts:
                data = client.graphql(cls.DISCOUNT_UPDATE, {
                    "id": discount["discount_id"], "discount": {"context": {"customers": customers}},
                })["discountCodeBasicUpdate"]
                cls.raise_user_errors(data["userErrors"], _("Shopify refused the discount update: %(detail)s"))

    @classmethod
    def _retire(cls, client: ShopifyClient, profile: ShopifyDiscountProfile) -> None:
        """Delete the profile's discounts in Shopify, then the profile."""
        for discount in profile.discounts:
            data = client.graphql(cls.DISCOUNT_DELETE, {"id": discount["discount_id"]})["discountCodeDelete"]
            # Deleted by hand in Shopify already is fine; anything else must not
            # leave a discount open to customers the ERP no longer offers it to.
            if data["userErrors"] and client.graphql(cls.DISCOUNT_QUERY, {"id": discount["discount_id"]})["codeDiscountNode"]:
                cls.raise_user_errors(data["userErrors"], _("Shopify refused to delete the discount: %(detail)s"))
        profile.delete()

    # ── Customers ────────────────────────────────────────────────────────

    @classmethod
    def _update_customers(
        cls,
        client: ShopifyClient,
        links: list[ShopifyCustomerLink],
        published: dict[int, ShopifyDiscountProfile],
        errors: dict[int, str],
        result: DiscountSyncResult,
    ) -> None:
        """Give each customer their profile's codes for the theme, or take them away, and record why some get none.

        ``links`` hold what they had before this run.
        """
        to_write = []
        for link in links:
            profile = published.get(link.pk)
            codes = json.dumps(profile.codes) if profile else ""
            if codes != link.discount_codes:
                to_write.append((link, profile, codes))
            if (profile.pk if profile else None) != link.discount_profile_id:
                result.changed += 1
            error = errors.get(link.pk, "")
            if error != link.discount_error:
                ShopifyCustomerLink.objects.filter(pk=link.pk).update(discount_error=error)
            if error:
                result.errors.append(f"{link.client}: {error}")
        per_call = cls.METAFIELD_BATCH // 2  # two metafields per customer
        for start in range(0, len(to_write), per_call):
            batch = to_write[start:start + per_call]
            cls._write_metafields(client, [(link, profile) for link, profile, _codes in batch if profile])
            cls._clear_metafields(client, [link for link, profile, _codes in batch if profile is None])
            for link, _profile, codes in batch:
                ShopifyCustomerLink.objects.filter(pk=link.pk).update(discount_codes=codes)
        result.offered = len(published)

    @classmethod
    def _write_metafields(
        cls, client: ShopifyClient, links: list[tuple[ShopifyCustomerLink, ShopifyDiscountProfile]]
    ) -> None:
        metafields = []
        for link, profile in links:
            product = next((d["percentage"] for d in profile.discounts if d["minimum"] is None), "0")
            metafields += [
                {"ownerId": link.customer_id, "key": cls.CODES_KEY,
                 "type": "list.single_line_text_field", "value": json.dumps(profile.codes)},
                {"ownerId": link.customer_id, "key": cls.PRODUCT_PERCENTAGE_KEY, "type": "number_decimal", "value": product},
            ]
        cls.set_metafields(client, metafields, _("Shopify refused to update the customer: %(detail)s"))

    @classmethod
    def _clear_metafields(cls, client: ShopifyClient, links: list[ShopifyCustomerLink]) -> None:
        metafields = [
            {"ownerId": link.customer_id, "key": key} for link in links for key in (cls.CODES_KEY, cls.PRODUCT_PERCENTAGE_KEY)
        ]
        cls.delete_metafields(client, metafields, _("Shopify refused to update the customer: %(detail)s"))
