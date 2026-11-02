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
from inventory.choices import ItemType, OnlineSaleMode
from inventory.models import InventoryItem, Warehouse
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
            self.status[product["id"]] = product["status"]
            self.status_updates.append((product["id"], product["status"]))
            return {"productUpdate": {"product": product, "userErrors": []}}
        if "variantsCount" in query:
            product_id = variables["id"]
            count = sum(1 for v in self.variants.values() if v["product"]["id"] == product_id)
            return {"product": {"status": self.status.get(product_id, "ACTIVE"), "variantsCount": {"count": count}}}
        if "productCreate" in query:
            self.created.append(variables["product"])
            variant = self._add(None, variables["product"]["title"])
            self.status[variant["product"]["id"]] = variables["product"]["status"]
            return {"productCreate": {"product": {**variant["product"], "variants": {"nodes": [variant]}}, "userErrors": []}}
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

    def test_the_stores_own_tags_are_left_alone(self):
        self.push()
        product = ShopifyVariantLink.objects.get(item=self.tank).product_id
        self.fake.tags[product] = {"summer"}
        self.set_mode(QuoteRequestMode.INSTEAD)
        self.push()
        self.assertEqual(self.tags(), {"summer", "request-quote", "quote-only"})
