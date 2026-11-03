import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from celery.exceptions import Retry
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import Http404
from django.test import RequestFactory, TestCase
from django.utils import timezone
from djmoney.money import Money

from api.fleet.tests.helpers import make_company, make_user
from apps.shopify.admin import ShopifyStoreAdmin, ShopifyVariantLinkAdmin
from apps.shopify.models import (
    MadeToMeasureMode,
    QuoteRequestMode,
    ShopifyLocationMap,
    ShopifyStore,
    ShopifyVariantLink,
    ShopifyWebhookEvent,
    VariantLinkStatus,
    WebhookEventStatus,
)
from apps.shopify.services.catalog import CatalogPushBusy, ShopifyCatalogService
from apps.shopify.services.push_scheduler import ShopifyPushScheduler
from apps.shopify.services.webhooks import ShopifyWebhookService
from apps.shopify.tasks import push_catalog_for_store
from company.models import CompanyDefaultTax, CompanyUser
from inventory.choices import ItemSchemaKind, ItemType, OnlineSaleMode
from configurator.models import ConfigurationPreset, ParameterType, ProductParameter, ProductTemplate
from inventory.models import InventoryItem, Warehouse
from inventory.tests.factories import make_item_schema
from invoices.models import AppliedTax, TaxType


def variant(n, sku, product=1, title="Default Title"):
    return {
        "id": f"gid://shopify/ProductVariant/{n}",
        "sku": sku,
        "title": title,
        "product": {"id": f"gid://shopify/Product/{product}", "title": f"Product {product}"},
        "inventoryItem": {"id": f"gid://shopify/InventoryItem/{n}"},
    }


def page(nodes, next_cursor=None):
    return {"productVariants": {"nodes": nodes, "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor}}}


def product_page(nodes):
    return {"product": {"variants": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}


GRAPHQL = "apps.shopify.client.ShopifyClient.graphql"


class CatalogMatchingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.other_company = make_company(name="Other")
        cls.warehouse = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.other_warehouse = Warehouse.objects.create(company=cls.other_company, code="X", name="X")
        cls.tank = InventoryItem.objects.create(company=cls.company, code="1010001", name="Tank 100 L", default_warehouse=cls.warehouse)
        cls.probe = InventoryItem.objects.create(company=cls.company, code="STK-00011", name="Probe", default_warehouse=cls.warehouse)
        # Same code in another company: must never be linked to this store.
        cls.foreign = InventoryItem.objects.create(
            company=cls.other_company, code="0990001", name="Foreign", item_type=ItemType.SERVICE
        )
        cls.store = ShopifyStore.objects.create(company=cls.company, name="Shop", shop_domain="fistex.myshopify.com")

    def link(self, n):
        return ShopifyVariantLink.objects.get(variant_id=f"gid://shopify/ProductVariant/{n}")

    @mock.patch(GRAPHQL)
    def test_variants_are_linked_by_sku_within_the_company(self, graphql):
        graphql.side_effect = [
            page([variant(1, "1010001"), variant(2, " stk-00011 ")], next_cursor="c1"),
            page([variant(3, "0990001"), variant(4, ""), variant(5, "DUP"), variant(6, "dup", product=2)]),
        ]
        counts = ShopifyCatalogService.sync_catalog(self.store)

        self.assertEqual((self.link(1).status, self.link(1).item), (VariantLinkStatus.MATCHED, self.tank))
        self.assertEqual(self.link(2).item, self.probe)  # whitespace and case don't matter
        self.assertEqual((self.link(3).status, self.link(3).item), (VariantLinkStatus.NOT_IN_ERP, None))
        self.assertEqual(self.link(4).status, VariantLinkStatus.NO_SKU)
        self.assertEqual({self.link(5).status, self.link(6).status}, {VariantLinkStatus.AMBIGUOUS})
        self.assertEqual(counts[VariantLinkStatus.MATCHED], 2)
        self.assertEqual(graphql.call_args_list[1].args[1]["after"], "c1")

    @mock.patch(GRAPHQL)
    def test_variants_removed_from_shopify_are_unlinked(self, graphql):
        graphql.side_effect = [page([variant(1, "1010001"), variant(2, "STK-00011")]), page([variant(1, "1010001")])]
        ShopifyCatalogService.sync_catalog(self.store)
        ShopifyCatalogService.sync_catalog(self.store)
        self.assertEqual(list(self.store.variant_links.values_list("sku", flat=True)), ["1010001"])

    @mock.patch(GRAPHQL)
    def test_items_can_swap_between_variants(self, graphql):
        graphql.side_effect = [
            page([variant(1, "1010001"), variant(2, "STK-00011")]),
            page([variant(1, "STK-00011"), variant(2, "1010001")]),
        ]
        ShopifyCatalogService.sync_catalog(self.store)
        ShopifyCatalogService.sync_catalog(self.store)
        self.assertEqual((self.link(1).item, self.link(2).item), (self.probe, self.tank))

    @mock.patch(GRAPHQL)
    def test_products_update_webhook_relinks_the_product(self, graphql):
        graphql.side_effect = [page([variant(1, "WRONG")]), product_page([variant(1, "1010001")])]
        ShopifyCatalogService.sync_catalog(self.store)
        self.assertEqual(self.link(1).status, VariantLinkStatus.NOT_IN_ERP)

        event = ShopifyWebhookEvent.objects.create(
            store=self.store, webhook_id="wh-1", topic="products/update",
            payload={"admin_graphql_api_id": "gid://shopify/Product/1"},
        )
        event = ShopifyWebhookService.process(event.pk)

        self.assertEqual(event.status, WebhookEventStatus.PROCESSED)
        self.assertEqual((self.link(1).status, self.link(1).item), (VariantLinkStatus.MATCHED, self.tank))

    @mock.patch(GRAPHQL)
    def test_deleted_product_loses_its_links(self, graphql):
        graphql.side_effect = [page([variant(1, "1010001")]), {"product": None}]
        ShopifyCatalogService.sync_catalog(self.store)
        ShopifyCatalogService.refresh_product(self.store, "gid://shopify/Product/1")
        self.assertFalse(self.store.variant_links.exists())

    @mock.patch(GRAPHQL)
    def test_location_sync_keeps_the_chosen_warehouse(self, graphql):
        nodes = [{"id": "gid://shopify/Location/1", "name": "Fistex Energy", "isActive": True}]
        graphql.return_value = {"locations": {"nodes": nodes}}
        ShopifyCatalogService.sync_locations(self.store)
        ShopifyLocationMap.objects.update(warehouse=self.warehouse)

        nodes[0]["name"] = "Fistex Energy (renamed)"
        ShopifyCatalogService.sync_locations(self.store)
        location = ShopifyLocationMap.objects.get()
        self.assertEqual((location.name, location.warehouse), ("Fistex Energy (renamed)", self.warehouse))

    def test_location_cannot_use_another_companys_warehouse(self):
        location = ShopifyLocationMap(store=self.store, location_id="gid://shopify/Location/1", name="L", warehouse=self.other_warehouse)
        with self.assertRaises(ValidationError):
            location.full_clean()

    def test_link_admin_lists_only_own_company_links(self):
        cache.clear()
        other_store = ShopifyStore.objects.create(company=self.other_company, name="Other", shop_domain="other.myshopify.com")
        own = ShopifyVariantLink.objects.create(store=self.store, product_id="p", variant_id="v1", synced_at="2026-10-20T10:00Z")
        ShopifyVariantLink.objects.create(store=other_store, product_id="p", variant_id="v2", synced_at="2026-10-20T10:00Z")
        user = make_user(username="fistex_staff")
        CompanyUser.objects.create(user=user, company=self.company, is_active=True)
        request = RequestFactory().get("/")
        request.user = user
        admin = ShopifyVariantLinkAdmin(ShopifyVariantLink, admin_site=None)
        self.assertEqual(list(admin.get_queryset(request)), [own])


class FakeCatalog:
    """A Shopify store's variants, answering the catalog push calls."""

    def __init__(self, existing=(), refuse_updates=False, taxes_included=False):
        self.variants = {}  # variant gid -> {"sku", "price", "cost", "tracked", "requiresShipping", "product"}
        for sku in existing:
            self._add(sku, title="Existing")
        self.created = []
        self.refuse_updates = refuse_updates
        self.taxes_included = taxes_included
        self.status = {}  # product gid -> status
        self.tags = {}  # product gid -> set of tags
        self.status_updates = []
        self.metafields = {}  # (owner gid, key) -> json value
        self.metafield_calls = 0
        self.product_types = {}  # product gid -> product type
        self.product_type_calls = 0
        self.deleted_variants = []
        self.stock = {}  # inventory item gid -> {location gid: on_hand}

    def _add(self, sku, title):
        n = len(self.variants) + 1
        variant = {"id": f"gid://shopify/ProductVariant/{n}", "sku": sku, "title": "Default Title",
                   "product": {"id": f"gid://shopify/Product/{n}", "title": title},
                   "inventoryItem": {"id": f"gid://shopify/InventoryItem/{n}"}}
        self.variants[variant["id"]] = variant
        return variant

    def __call__(self, query, variables=None):
        if "taxesIncluded" in query:
            return {"shop": {"taxesIncluded": self.taxes_included}}
        if "tagsAdd" in query or "tagsRemove" in query:
            tags = self.tags.setdefault(variables["id"], set())
            if "tagsAdd" in query:
                tags.update(variables["tags"])
                return {"tagsAdd": {"userErrors": []}}
            tags.difference_update(variables["tags"])
            return {"tagsRemove": {"userErrors": []}}
        if "productUpdate" in query:
            product = variables["product"]
            if "productType" in product:
                self.product_types[product["id"]] = product["productType"]
                self.product_type_calls += 1
            if "status" in product:
                self.status[product["id"]] = product["status"]
                self.status_updates.append((product["id"], product["status"]))
            return {"productUpdate": {"product": product, "userErrors": []}}
        if "variantsCount" in query:
            product_id = variables["id"]
            count = sum(1 for v in self.variants.values() if v["product"]["id"] == product_id)
            return {"product": {"status": self.status.get(product_id, "ACTIVE"), "variantsCount": {"count": count}}}
        if "productCreate" in query:
            product = variables["product"]
            self.created.append(product)
            variant = self._add(None, product["title"])
            variant["options"] = [option["values"][0]["name"] for option in product.get("productOptions", [])]
            self.status[variant["product"]["id"]] = product["status"]
            return {"productCreate": {"product": {**variant["product"], "variants": {"nodes": [variant]}}, "userErrors": []}}
        if "productVariantsBulkCreate" in query:
            created = []
            for new in variables["variants"]:
                variant = self._add(None, "")
                variant["product"] = next(
                    v["product"] for v in self.variants.values() if v["product"]["id"] == variables["productId"]
                )
                variant["options"] = [value["name"] for value in new["optionValues"]]
                created.append(variant)
            return {"productVariantsBulkCreate": {"productVariants": created, "userErrors": []}}
        if "productVariantsBulkDelete" in query:
            for variant_id in variables["variantsIds"]:
                del self.variants[variant_id]
            self.deleted_variants.extend(variables["variantsIds"])
            return {"productVariantsBulkDelete": {"userErrors": []}}
        if "metafieldsSet" in query or "metafieldsDelete" in query:
            self.metafield_calls += 1
            for metafield in variables["metafields"]:
                assert metafield["namespace"] == "polipus"
                key = (metafield["ownerId"], metafield["key"])
                if "metafieldsSet" in query:
                    assert metafield["type"] == "json"
                    self.metafields[key] = json.loads(metafield["value"])
                else:
                    self.metafields.pop(key, None)
            return {"metafieldsSet" if "metafieldsSet" in query else "metafieldsDelete": {"userErrors": []}}
        if "inventoryPolicy inventoryItem" in query:  # a variant's stock settings
            variant = self.variants[variables["id"]]
            return {"productVariant": {
                "inventoryPolicy": variant.get("inventoryPolicy", "DENY"), "inventoryItem": {"tracked": variant.get("tracked", False)},
            }}
        if "inventoryLevels" in query:
            levels = self.stock.get(variables["id"], {})
            return {"inventoryItem": {"inventoryLevels": {"nodes": [
                {"location": {"id": location}, "quantities": [{"name": "on_hand", "quantity": quantity}]}
                for location, quantity in levels.items()
            ]}}}
        if "inventorySetQuantities" in query:
            for change in variables["input"]["quantities"]:
                levels = self.stock[change["inventoryItemId"]]
                assert levels[change["locationId"]] == change["changeFromQuantity"]
                levels[change["locationId"]] = change["quantity"]
            return {"inventorySetQuantities": {"userErrors": []}}
        if "productVariantsBulkUpdate" in query:
            if self.refuse_updates:
                return {"productVariantsBulkUpdate": {"userErrors": [{"field": None, "message": "Price is invalid"}]}}
            for change in variables["variants"]:
                variant = self.variants[change["id"]]
                variant.update({key: value for key, value in change.items() if key != "inventoryItem"})
                variant.update(change.get("inventoryItem", {}))
            return {"productVariantsBulkUpdate": {"userErrors": []}}
        wanted = variables["query"].split('"')[1].upper()
        return {"productVariants": {"nodes": [v for v in self.variants.values() if (v["sku"] or "").upper() == wanted]}}


class CatalogPushTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.tank = InventoryItem.objects.create(
            company=cls.company, code="T-1", name="Tank 100 L", description="Steel tank\nFor hot water",
            default_warehouse=cls.nave, sell_online=True,
            selling_price=Money("500.81", "EUR"), cost_price=Money("300.00", "EUR"),
        )
        cls.service = InventoryItem.objects.create(
            company=cls.company, code="SRV-9", name="Installation", item_type=ItemType.SERVICE,
            sell_online=True, selling_price=Money("80.00", "EUR"),
        )
        cls.offline = InventoryItem.objects.create(company=cls.company, code="P-1", name="Spare part", default_warehouse=cls.nave)
        # Another company's item sold online never reaches this store.
        InventoryItem.objects.create(company=make_company(name="Other"), code="T-1", name="Foreign", item_type=ItemType.SERVICE, sell_online=True)

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com",
            access_token_encrypted="x", push_catalog=True,
        )

    def push(self, fake, item_ids=None):
        with mock.patch(GRAPHQL, side_effect=fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push") as stock:
            return ShopifyCatalogService.push_catalog(self.store, item_ids), stock

    def test_items_sold_online_become_draft_products_with_erp_price_cost_and_sku(self):
        fake = FakeCatalog()
        result, stock = self.push(fake)

        self.assertEqual((result.created, result.updated, result.errors), (2, 0, []))
        self.assertEqual([p["title"] for p in fake.created], ["Tank 100 L", "Installation"])
        self.assertEqual({p["status"] for p in fake.created}, {"DRAFT"})
        self.assertEqual(fake.created[0]["descriptionHtml"], "<p>Steel tank<br>For hot water</p>")
        tank = fake.variants[ShopifyVariantLink.objects.get(item=self.tank).variant_id]
        self.assertEqual((tank["sku"], tank["price"], tank["cost"]), ("T-1", "500.81", "300.00"))
        self.assertEqual((tank["tracked"], tank["requiresShipping"]), (True, True))
        installation = fake.variants[ShopifyVariantLink.objects.get(item=self.service).variant_id]
        self.assertEqual((installation["tracked"], installation["requiresShipping"]), (False, False))
        self.assertFalse(ShopifyVariantLink.objects.filter(item=self.offline).exists())
        stock.assert_any_call([self.tank.pk])  # new products get their stock right away

    def test_existing_shopify_product_with_the_sku_is_linked_not_duplicated(self):
        fake = FakeCatalog(existing=["t-1"])
        result, _ = self.push(fake, item_ids=[self.tank.pk])
        self.assertEqual((result.created, result.updated), (0, 1))
        self.assertEqual(fake.created, [])
        self.assertEqual(ShopifyVariantLink.objects.get(item=self.tank).status, VariantLinkStatus.MATCHED)

    def test_unchanged_items_are_skipped_and_changes_are_sent(self):
        fake = FakeCatalog()
        self.push(fake)
        result, _ = self.push(fake)
        self.assertEqual((result.created, result.updated, result.unchanged), (0, 0, 2))

        InventoryItem.objects.filter(pk=self.tank.pk).update(selling_price=Decimal("520.00"))
        result, _ = self.push(fake)
        self.assertEqual((result.updated, result.unchanged), (1, 1))
        self.assertEqual(fake.variants[ShopifyVariantLink.objects.get(item=self.tank).variant_id]["price"], "520.00")

    def test_failure_after_creating_is_retried_as_an_update(self):
        fake = FakeCatalog(refuse_updates=True)
        result, _ = self.push(fake, item_ids=[self.tank.pk])
        self.assertEqual((len(fake.created), len(result.errors)), (1, 1))

        fake.refuse_updates = False
        result, _ = self.push(fake, item_ids=[self.tank.pk])
        self.assertEqual((len(fake.created), result.updated), (1, 1))  # no second product
        tank = fake.variants[ShopifyVariantLink.objects.get(item=self.tank).variant_id]
        self.assertEqual((tank["price"], tank["tracked"]), ("500.81", True))  # setup retried too

    def test_products_not_created_by_the_erp_keep_their_shopify_settings(self):
        fake = FakeCatalog(existing=["T-1"])
        self.push(fake, item_ids=[self.tank.pk])
        tank = fake.variants[ShopifyVariantLink.objects.get(item=self.tank).variant_id]
        self.assertEqual(tank["price"], "500.81")
        self.assertNotIn("tracked", tank)

    def test_store_without_catalog_push_and_concurrent_pushes(self):
        self.store.push_catalog = False
        with mock.patch(GRAPHQL) as graphql:
            self.assertEqual(ShopifyCatalogService.push_catalog(self.store).created, 0)
        graphql.assert_not_called()

        self.store.push_catalog = True
        cache.add(ShopifyCatalogService.PUSH_LOCK.format(store_id=self.store.pk), True)
        with self.assertRaises(CatalogPushBusy):
            ShopifyCatalogService.push_catalog(self.store)


class CatalogPushSchedulingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = make_company(name="Fistex")
        cls.item = InventoryItem.objects.create(company=company, code="T-1", name="Tank", item_type=ItemType.SERVICE)
        cls.offline = InventoryItem.objects.create(company=company, code="P-1", name="Part", item_type=ItemType.SERVICE)
        ShopifyStore.objects.create(
            company=company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x", push_catalog=True
        )

    def setUp(self):
        cache.clear()

    def test_saving_an_item_sold_online_queues_its_push_after_commit(self):
        with mock.patch("apps.shopify.tasks.push_catalog_for_items.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.item.sell_online = True
                self.item.save()
                self.offline.save()
                delay.assert_not_called()  # nothing leaves before commit
        delay.assert_called_once_with([self.item.pk])


class CatalogPushInBackgroundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.other = make_company(name="Other")
        cls.user = make_user(username="staff")
        cls.user.user_permissions.add(Permission.objects.get(codename="change_shopifystore"))
        CompanyUser.objects.create(user=cls.user, company=cls.company, is_active=True)
        InventoryItem.objects.create(
            company=cls.company, code="SRV-9", name="Installation", item_type=ItemType.SERVICE,
            sell_online=True, selling_price=Money("80.00", "EUR"),
        )

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com",
            access_token_encrypted="x", push_catalog=True,
        )
        self.admin = ShopifyStoreAdmin(ShopifyStore, admin_site=None)

    def click(self, store=None):
        request = RequestFactory().post("/")
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        self.admin.push_catalog(request, (store or self.store).pk)
        self.store.refresh_from_db()
        return [str(message) for message in request._messages]

    def test_the_button_queues_the_push_and_the_page_shows_its_outcome(self):
        with mock.patch("apps.shopify.tasks.push_catalog_for_store.delay") as delay:
            self.click()
            self.assertTrue(self.store.catalog_push_running)
            self.assertIn("…", str(self.admin.last_catalog_push(self.store)))
            self.click()  # a second click while it runs queues nothing more
        delay.assert_called_once_with(self.store.pk)

        with mock.patch(GRAPHQL, side_effect=FakeCatalog()), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            push_catalog_for_store(self.store.pk)
        self.store.refresh_from_db()
        self.assertFalse(self.store.catalog_push_running)
        self.assertEqual(self.store.catalog_push_result["created"], 1)
        self.assertIn("1", str(self.admin.last_catalog_push(self.store)))

    def test_items_that_failed_are_listed_on_the_page(self):
        with mock.patch(GRAPHQL, side_effect=FakeCatalog(refuse_updates=True)):
            ShopifyCatalogService.push_whole_catalog(self.store)
        self.assertEqual(self.store.catalog_push_result["error_count"], 1)
        self.assertIn("SRV-9", str(self.admin.last_catalog_push(self.store)))

    def test_a_queue_outage_is_reported_instead_of_waiting_forever(self):
        with mock.patch("apps.shopify.tasks.push_catalog_for_store.delay", side_effect=ConnectionError):
            self.click()
        self.assertFalse(self.store.catalog_push_running)
        self.assertIn("failed", self.store.catalog_push_result)

    def test_a_push_that_never_finished_stops_counting_as_running(self):
        ShopifyStore.objects.filter(pk=self.store.pk).update(catalog_push_started_at=timezone.now() - timedelta(hours=2))
        self.store.refresh_from_db()
        self.assertFalse(self.store.catalog_push_running)

    def test_a_push_already_running_makes_the_task_try_again_later(self):
        cache.add(ShopifyCatalogService.PUSH_LOCK.format(store_id=self.store.pk), True)
        with mock.patch.object(push_catalog_for_store, "retry", side_effect=Retry()) as retry, mock.patch(GRAPHQL) as graphql:
            with self.assertRaises(Retry):
                push_catalog_for_store(self.store.pk)
        retry.assert_called_once()
        graphql.assert_not_called()

    def test_another_companys_store_cannot_be_pushed(self):
        foreign = ShopifyStore.objects.create(
            company=self.other, name="B", shop_domain="b.myshopify.com", access_token_encrypted="x", push_catalog=True
        )
        with mock.patch("apps.shopify.tasks.push_catalog_for_store.delay") as delay, self.assertRaises(Http404):
            self.click(foreign)
        delay.assert_not_called()


class ArchiveUnsoldProductsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.tank = InventoryItem.objects.create(
            company=cls.company, code="T-1", name="Tank", item_type=ItemType.SERVICE,
            selling_price=Money("500.00", "EUR"),
        )
        # Without the save signal, whose push would wait for this class's transaction.
        InventoryItem.objects.filter(pk=cls.tank.pk).update(sell_online=True)
        cls.tank.sell_online = True

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com",
            access_token_encrypted="x", push_catalog=True,
        )
        self.fake = FakeCatalog()

    def push(self, item_ids=None):
        with mock.patch(GRAPHQL, side_effect=self.fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            return ShopifyCatalogService.push_catalog(self.store, item_ids)

    def sell_online(self, value):
        InventoryItem.objects.filter(pk=self.tank.pk).update(sell_online=value)

    def product(self):
        return ShopifyVariantLink.objects.get(item=self.tank).product_id

    def test_unticking_archives_the_product_and_ticking_again_restores_its_status(self):
        self.push()
        self.fake.status[self.product()] = "ACTIVE"  # published in Shopify meanwhile

        self.sell_online(False)
        self.assertEqual(self.push().archived, 1)
        self.assertEqual(self.fake.status[self.product()], "ARCHIVED")
        self.assertEqual(self.push().archived, 0)  # already archived: nothing sent again

        self.sell_online(True)
        result = self.push()
        self.assertEqual((result.restored, self.fake.status[self.product()]), (1, "ACTIVE"))
        self.assertEqual(ShopifyVariantLink.objects.get(item=self.tank).archived_from, "")

    def test_products_the_erp_never_pushed_are_left_alone(self):
        self.fake._add("T-1", title="Sold in Shopify")
        self.sell_online(False)
        ShopifyCatalogService._save_variants(self.store, list(self.fake.variants.values()))
        ShopifyCatalogService._match(self.store)
        self.assertEqual(self.push().archived, 0)
        self.assertEqual(self.fake.status_updates, [])

    def test_a_product_with_variants_the_erp_does_not_sell_stays_online(self):
        self.push()
        product = self.fake.variants[ShopifyVariantLink.objects.get(item=self.tank).variant_id]["product"]
        self.fake.variants["gid://shopify/ProductVariant/99"] = {
            "id": "gid://shopify/ProductVariant/99", "sku": "OTHER", "title": "Big", "product": product,
            "inventoryItem": {"id": "gid://shopify/InventoryItem/99"},
        }
        self.sell_online(False)
        self.assertEqual(self.push().archived, 0)
        self.assertEqual(self.fake.status_updates, [])

    def test_a_product_archived_by_hand_stays_archived_when_sold_online_again(self):
        self.push()
        self.fake.status[self.product()] = "ARCHIVED"
        self.sell_online(False)
        self.push()
        self.sell_online(True)
        self.assertEqual(self.push().restored, 0)
        self.assertEqual((self.fake.status_updates, self.fake.status[self.product()]), ([], "ARCHIVED"))

    def test_unticking_an_item_queues_its_push_and_unpushed_items_queue_nothing(self):
        self.push()
        offline = InventoryItem.objects.create(company=self.company, code="P-1", name="Part", item_type=ItemType.SERVICE)
        with mock.patch("apps.shopify.tasks.push_catalog_for_items.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.tank.sell_online = False
                self.tank.save()
                offline.save()
        delay.assert_called_once_with([self.tank.pk])


class TaxInclusivePricesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.iva = TaxType.objects.create(tax_name="IVA", behavior="add")
        cls.irpf = TaxType.objects.create(tax_name="IRPF", behavior="subtract")
        cls.tank = cls.item("T-1", "500.81")
        item_ct = ContentType.objects.get_for_model(InventoryItem)
        AppliedTax.objects.create(content_type=item_ct, object_id=cls.tank.pk, tax_type=cls.iva, selected_percentage=21)
        # A retention applies between businesses, never to a consumer's price.
        AppliedTax.objects.create(content_type=item_ct, object_id=cls.tank.pk, tax_type=cls.irpf, selected_percentage=15)
        cls.book = cls.item("B-1", "20.00")
        AppliedTax.objects.create(content_type=item_ct, object_id=cls.book.pk, tax_type=cls.iva, selected_percentage=4)
        cls.part = cls.item("P-1", "10.00")  # no tax of its own

    @classmethod
    def item(cls, code, price):
        item = InventoryItem.objects.create(
            company=cls.company, code=code, name=code, item_type=ItemType.SERVICE, selling_price=Money(price, "EUR")
        )
        InventoryItem.objects.filter(pk=item.pk).update(sell_online=True)  # without the save signal
        return item

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com",
            access_token_encrypted="x", push_catalog=True,
        )

    def push(self, fake):
        with mock.patch(GRAPHQL, side_effect=fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            return ShopifyCatalogService.push_catalog(self.store)

    def price(self, fake, item):
        return fake.variants[ShopifyVariantLink.objects.get(item=item).variant_id]["price"]

    def test_tax_is_added_where_the_store_shows_prices_with_tax(self):
        fake = FakeCatalog(taxes_included=True)
        result = self.push(fake)
        self.assertEqual((self.price(fake, self.tank), self.price(fake, self.book)), ("605.98", "20.80"))
        # No tax and no company default: never guessed, and no product is created.
        self.assertEqual(len(result.errors), 1)
        self.assertIn("P-1", result.errors[0])
        self.assertFalse(ShopifyVariantLink.objects.filter(item=self.part).exists())

        CompanyDefaultTax.objects.create(company=self.company, tax_type=self.iva, selected_percentage=21)
        self.push(fake)
        self.assertEqual(self.price(fake, self.part), "12.10")

    def test_stores_without_tax_in_prices_get_the_net_price_and_a_switch_resends_them(self):
        fake = FakeCatalog(taxes_included=False)
        self.assertEqual(self.push(fake).errors, [])  # no tax needed
        self.assertEqual(self.price(fake, self.tank), "500.81")

        fake.taxes_included = True
        result = self.push(fake)
        self.assertEqual(result.updated, 2)
        self.assertEqual(self.price(fake, self.tank), "605.98")


class SaleModeTagsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.tank = InventoryItem.objects.create(
            company=cls.company, code="T-1", name="Tank", item_type=ItemType.SERVICE, selling_price=Money("500.00", "EUR"),
        )
        InventoryItem.objects.filter(pk=cls.tank.pk).update(sell_online=True)  # without the save signal

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x", push_catalog=True,
        )
        self.fake = FakeCatalog()

    def push(self):
        with mock.patch(GRAPHQL, side_effect=self.fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            return ShopifyCatalogService.push_catalog(self.store)

    def tags(self):
        return self.fake.tags.get(ShopifyVariantLink.objects.get(item=self.tank).product_id, set())

    def set_mode(self, quote_requests, item_mode=OnlineSaleMode.BUY):
        self.store.quote_requests = quote_requests
        self.store.save()
        InventoryItem.objects.filter(pk=self.tank.pk).update(online_sale_mode=item_mode)

    def test_products_are_tagged_for_the_theme_by_how_they_are_sold(self):
        self.push()
        self.assertEqual(self.tags(), set())  # stores without quote requests

        self.set_mode(QuoteRequestMode.ALONGSIDE, OnlineSaleMode.BOTH)
        self.assertEqual(self.push().updated, 1)
        self.assertEqual(self.tags(), {"request-quote"})

        self.set_mode(QuoteRequestMode.INSTEAD)  # every product, whatever the item says
        self.push()
        self.assertEqual(self.tags(), {"request-quote", "quote-only"})

        self.set_mode(QuoteRequestMode.OFF, OnlineSaleMode.QUOTE)
        self.assertEqual(self.push().updated, 1)
        self.assertEqual(self.tags(), set())

    def test_a_quote_only_variant_cannot_be_bought_in_shopify_and_is_restored_when_sold_again(self):
        self.push()
        link = ShopifyVariantLink.objects.get(item=self.tank)
        variant = self.fake.variants[link.variant_id]
        variant.update(tracked=False, inventoryPolicy="CONTINUE")  # as staff had set it in Shopify
        self.fake.stock[link.inventory_item_id] = {"gid://shopify/Location/1": 4, "gid://shopify/Location/2": 0}

        self.set_mode(QuoteRequestMode.ALONGSIDE, OnlineSaleMode.QUOTE)
        self.push()

        self.assertEqual((variant["tracked"], variant["inventoryPolicy"]), (True, "DENY"))
        self.assertEqual(self.fake.stock[link.inventory_item_id], {"gid://shopify/Location/1": 0, "gid://shopify/Location/2": 0})
        link.refresh_from_db()
        self.assertEqual(link.quote_only_restore, {"tracked": False, "inventoryPolicy": "CONTINUE"})

        self.set_mode(QuoteRequestMode.ALONGSIDE, OnlineSaleMode.BOTH)
        with mock.patch(GRAPHQL, side_effect=self.fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push") as stock:
            ShopifyCatalogService.push_catalog(self.store)

        self.assertEqual((variant["tracked"], variant["inventoryPolicy"]), (False, "CONTINUE"))
        link.refresh_from_db()
        self.assertIsNone(link.quote_only_restore)
        stock.assert_called_once_with([self.tank.pk])  # the real stock replaces the zero it was held at

    def test_a_new_quote_only_product_is_created_unsellable(self):
        self.set_mode(QuoteRequestMode.INSTEAD)

        self.push()

        link = ShopifyVariantLink.objects.get(item=self.tank)
        variant = self.fake.variants[link.variant_id]
        self.assertEqual((variant["tracked"], variant["inventoryPolicy"]), (True, "DENY"))
        # Sold again, it gets what the ERP gives a new product, not Shopify's defaults.
        self.assertEqual(link.quote_only_restore, {"tracked": False, "inventoryPolicy": "DENY"})  # a service ships nothing

    def test_the_stores_own_tags_are_left_alone(self):
        self.push()
        product = ShopifyVariantLink.objects.get(item=self.tank).product_id
        self.fake.tags[product] = {"summer"}
        self.set_mode(QuoteRequestMode.INSTEAD)
        self.push()
        self.assertEqual(self.tags(), {"summer", "request-quote", "quote-only"})


class ProductCategoryTests(TestCase):
    """The product type taken from the item schema field the store names as its category field."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        block = make_item_schema(company=cls.company, name="Tienda online", kind=ItemSchemaKind.BLOCK, fields=[
            {"key": "category", "label": "Categoría", "type": "select", "options": ["Captadores solares", "Depósitos"]},
        ])
        schema = make_item_schema(company=cls.company, name="Captador", includes=[block])
        cls.collector = InventoryItem.objects.create(
            company=cls.company, code="C-1", name="Collector", item_type=ItemType.SERVICE, sell_online=True,
            selling_price=Money("400.00", "EUR"), metadata_schema=schema, metadata={"category": "Captadores solares"},
        )

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            push_catalog=True,
        )
        self.fake = FakeCatalog()

    def push(self):
        with mock.patch(GRAPHQL, side_effect=self.fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            return ShopifyCatalogService.push_catalog(self.store)

    def product_type(self):
        return self.fake.product_types.get(ShopifyVariantLink.objects.get(item=self.collector).product_id)

    def test_without_a_category_field_shopify_keeps_its_product_types(self):
        self.push()

        self.assertEqual(self.fake.product_type_calls, 0)

    def test_the_category_becomes_the_product_type_once_and_follows_changes(self):
        self.store.category_field = "category"
        self.store.save()

        self.push()
        self.assertEqual(self.product_type(), "Captadores solares")
        self.assertEqual(self.push().unchanged, 1)
        self.assertEqual(self.fake.product_type_calls, 1)

        InventoryItem.objects.filter(pk=self.collector.pk).update(metadata={"category": "Depósitos"})
        self.assertEqual(self.push().updated, 1)
        self.assertEqual(self.product_type(), "Depósitos")

        InventoryItem.objects.filter(pk=self.collector.pk).update(metadata={})
        self.push()
        self.assertEqual(self.product_type(), "")  # an item without a category leaves no stale one

    def test_the_category_is_not_repeated_among_the_specifications(self):
        self.push()
        variant_id = ShopifyVariantLink.objects.get(item=self.collector).variant_id
        self.assertEqual(self.fake.metafields[(variant_id, "specifications")][0]["key"], "category")

        self.store.category_field = "category"
        self.store.save()
        self.push()

        self.assertNotIn((variant_id, "specifications"), self.fake.metafields)  # the category was its only value


class VariantGroupTests(TestCase):
    """Items made from presets of one configurator template, sold as variants of one product."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        template = ProductTemplate.objects.create(company=cls.company, name="CA/TA", description="Hot water tanks")
        ProductParameter.objects.create(product=template, name="VOLUMEN", label="Volumen (L)", type=ParameterType.SELECT,
                                        options=[160, 500, 1000], order=0)
        ProductParameter.objects.create(product=template, name="RECUBRIMIENTO", label="Recubrimiento",
                                        type=ParameterType.SELECT, options=["F.Poliester", "F.Skay"], order=1)
        preset_type = ContentType.objects.get_for_model(ConfigurationPreset)

        def tank(code, volume, finish, price):
            preset = ConfigurationPreset.objects.create(
                company=cls.company, product=template, name=f"CA/TA-{volume}-{finish}",
                parameter_values={"VOLUMEN": volume, "RECUBRIMIENTO": finish},
            )
            return InventoryItem.objects.create(
                company=cls.company, code=code, name=f"Acumulador ACS Mod. {volume} CA/TA ({finish})",
                description=f"{volume} L tank",
                item_type=ItemType.SERVICE, sell_online=True, selling_price=Money(price, "EUR"),
                product_content_type=preset_type, product_object_id=preset.pk,
            )

        # Created out of order: the product still lists its sizes in the template's order.
        cls.big = tank("T-1000", "1000", "F.Skay", "4000.00")
        cls.small = tank("T-160", "160", "F.Skay", "1400.00")
        cls.small_polyester = tank("T-160P", "160", "F.Poliester", "1350.00")
        cls.valve = InventoryItem.objects.create(
            company=cls.company, code="V-1", name="Valve", item_type=ItemType.SERVICE, sell_online=True,
            selling_price=Money("20.00", "EUR"),
        )

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            push_catalog=True, group_variants=True, quote_requests=QuoteRequestMode.ALONGSIDE,
        )
        self.fake = FakeCatalog()

    def push(self, item_ids=None):
        with mock.patch(GRAPHQL, side_effect=self.fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            return ShopifyCatalogService.push_catalog(self.store, item_ids)

    def variant(self, item):
        return self.fake.variants[ShopifyVariantLink.objects.get(item=item).variant_id]

    def test_items_of_one_template_become_variants_of_one_product(self):
        result = self.push()

        self.assertEqual(result.created, 4)
        self.assertEqual(len(self.fake.created), 2)  # the tanks' product and the valve's
        tanks, valve = sorted(self.fake.created, key=lambda product: "productOptions" not in product)
        self.assertEqual(tanks["title"], "Acumulador ACS Mod. CA/TA")
        self.assertEqual(tanks["descriptionHtml"], "<p>Hot water tanks</p>")  # the template's, not one size's
        self.assertEqual(tanks["productOptions"], [
            {"name": "Volumen (L)", "values": [{"name": "160"}]},
            {"name": "Recubrimiento", "values": [{"name": "F.Poliester"}]},
        ])
        self.assertEqual(
            {self.variant(item)["product"]["id"] for item in (self.big, self.small, self.small_polyester)}, {
                self.variant(self.small)["product"]["id"]
            },
        )
        self.assertEqual(self.variant(self.big)["options"], ["1000", "F.Skay"])
        self.assertEqual((self.variant(self.big)["price"], self.variant(self.big)["sku"]), ("4000.00", "T-1000"))
        self.assertEqual(valve["title"], "Valve")

    def test_a_new_item_joins_its_groups_product_and_leaves_it_when_no_longer_sold(self):
        self.push()
        product = self.variant(self.small)["product"]["id"]
        InventoryItem.objects.filter(pk=self.big.pk).update(sell_online=False)

        result = self.push()

        self.assertEqual(result.removed, 1)
        self.assertEqual(result.archived, 0)
        self.assertFalse(ShopifyVariantLink.objects.filter(item=self.big).exists())
        self.assertEqual(self.fake.status[product], "DRAFT")  # the product stays for the other sizes

        InventoryItem.objects.filter(pk=self.big.pk).update(sell_online=True)
        self.assertEqual(self.push().created, 1)
        self.assertEqual(self.variant(self.big)["product"]["id"], product)

    def test_a_variant_only_linked_by_its_sku_is_never_deleted(self):
        # A product the merchant built in Shopify, its variants linked to ERP items by SKU.
        self.fake._add("T-1000", "Merchant's tank")["product"]["id"] = "gid://shopify/Product/merchant"
        self.fake._add("T-160", "Merchant's tank")["product"]["id"] = "gid://shopify/Product/merchant"
        self.store.group_variants = False
        self.store.save()
        InventoryItem.objects.filter(pk__in=[self.small_polyester.pk, self.valve.pk]).update(sell_online=False)
        self.push()
        InventoryItem.objects.filter(pk=self.big.pk).update(sell_online=False)

        result = self.push()

        self.assertEqual((result.removed, self.fake.deleted_variants), (0, []))
        self.assertTrue(ShopifyVariantLink.objects.filter(item=self.big).exists())

    def test_the_product_is_tagged_by_how_all_its_variants_sell(self):
        InventoryItem.objects.filter(pk=self.big.pk).update(online_sale_mode=OnlineSaleMode.QUOTE)

        self.push()

        product = self.variant(self.small)["product"]["id"]
        self.assertEqual(self.fake.tags[product], {"request-quote"})  # quotable, but some sizes can be bought
        variant_id = ShopifyVariantLink.objects.get(item=self.big).variant_id
        self.assertEqual(self.fake.metafields[(variant_id, "sale_mode")], OnlineSaleMode.QUOTE)

    def test_stores_that_dont_group_keep_one_product_per_item(self):
        self.store.group_variants = False
        self.store.save()

        self.push()

        self.assertEqual(len(self.fake.created), 4)
        self.assertTrue(all("productOptions" not in product for product in self.fake.created))


class ProductFieldsTests(TestCase):
    """Specifications (item schemas) and made-to-measure forms published on each item's variant."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        schema = make_item_schema(company=cls.company, name="Depósito", fields=[
            {"key": "capacity", "label": "Capacidad", "type": "number", "unit": "L"},
            {"key": "insulation", "label": "Aislamiento"},
            {"key": "coil", "label": "Serpentín"},
        ])
        cls.tank = InventoryItem.objects.create(
            company=cls.company, code="T-1", name="Tank 300 L", item_type=ItemType.SERVICE, sell_online=True,
            selling_price=Money("900.00", "EUR"), metadata_schema=schema,
            metadata={"capacity": 300, "insulation": "Poliuretano", "coil": ""},
        )
        cls.plain = InventoryItem.objects.create(
            company=cls.company, code="P-1", name="Valve", item_type=ItemType.SERVICE, sell_online=True,
            selling_price=Money("20.00", "EUR"),
        )
        template = ProductTemplate.objects.create(company=cls.company, name="CA/TA")
        ProductParameter.objects.create(product=template, name="VOLUMEN", label="Volumen (L)", type=ParameterType.SELECT,
                                        options=[160, 200], order=0, default_value="160")
        ProductParameter.objects.create(product=template, name="RECUBRIMIENTO", label="Recubrimiento",
                                        type=ParameterType.SELECT, options=["F.Skay"], order=1)
        preset = ConfigurationPreset.objects.create(
            company=cls.company, product=template, name="CA/TA a medida", parameter_values={"RECUBRIMIENTO": "F.Skay"},
        )
        cls.custom = InventoryItem.objects.create(
            company=cls.company, code="CATA", name="CA/TA a medida", item_type=ItemType.OWN_PRODUCTION, sell_online=True,
            selling_price=Money("0.00", "EUR"),
            product_content_type=ContentType.objects.get_for_model(ConfigurationPreset), product_object_id=preset.pk,
        )

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            push_catalog=True, quote_requests=QuoteRequestMode.ALONGSIDE,
        )
        self.fake = FakeCatalog()

    def push(self):
        with mock.patch(GRAPHQL, side_effect=self.fake), mock.patch.object(ShopifyPushScheduler, "schedule_stock_push"):
            return ShopifyCatalogService.push_catalog(self.store)

    def field(self, item, key):
        return self.fake.metafields.get((ShopifyVariantLink.objects.get(item=item).variant_id, key))

    def test_specifications_follow_the_schema_and_leave_out_empty_values(self):
        self.push()

        self.assertEqual(self.field(self.tank, "specifications"), [
            {"key": "capacity", "label": "Capacidad", "type": "number", "unit": "L", "value": 300},
            {"key": "insulation", "label": "Aislamiento", "type": "text", "unit": "", "value": "Poliuretano"},
        ])
        self.assertIsNone(self.field(self.plain, "specifications"))

    def test_fields_are_sent_once_and_follow_changes(self):
        self.push()
        calls = self.fake.metafield_calls

        self.push()
        self.assertEqual(self.fake.metafield_calls, calls)  # nothing changed, nothing sent

        InventoryItem.objects.filter(pk=self.tank.pk).update(metadata={"capacity": 500})
        self.push()
        self.assertEqual([spec["value"] for spec in self.field(self.tank, "specifications")], [500])

        InventoryItem.objects.filter(pk=self.tank.pk).update(metadata_schema=None)
        self.push()
        self.assertIsNone(self.field(self.tank, "specifications"))

    def test_every_variant_says_how_it_sells_in_one_call(self):
        InventoryItem.objects.filter(pk__in=[self.tank.pk, self.custom.pk]).update(sell_online=False)

        self.push()

        self.assertEqual(self.field(self.plain, "sale_mode"), OnlineSaleMode.BUY)
        self.assertEqual(self.fake.metafield_calls, 1)

    def test_made_to_measure_items_publish_their_open_parameters_and_are_quote_only(self):
        self.push()
        self.assertIsNone(self.field(self.custom, "made_to_measure"))  # the store doesn't take them yet

        self.store.made_to_measure = MadeToMeasureMode.QUOTE
        self.store.save()
        self.push()

        self.assertEqual(self.field(self.custom, "made_to_measure"), {
            "fixed": [{"label": "Recubrimiento", "value": "F.Skay"}],
            "parameters": [{"name": "VOLUMEN", "label": "Volumen (L)", "type": "select", "options": ["160", "200"], "default": "160"}],
        })
        product_id = ShopifyVariantLink.objects.get(item=self.custom).product_id
        self.assertIn(ShopifyCatalogService.QUOTE_ONLY_TAG, self.fake.tags[product_id])
        self.assertIsNone(self.field(self.tank, "made_to_measure"))
