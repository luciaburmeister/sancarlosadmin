from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase

from api.fleet.tests.helpers import make_company
from apps.shopify.exceptions import ShopifyAPIError
from apps.shopify.models import QuoteRequestMode, ShopifyLocationMap, ShopifyStore, ShopifyVariantLink, VariantLinkStatus
from apps.shopify.services.push_scheduler import ShopifyPushScheduler
from apps.shopify.services.stock import ShopifyStockService
from apps.shopify.tasks import push_stock_for_items
from company.models import Company
from inventory.choices import ItemType, MovementType, OnlineSaleMode
from inventory.models import InventoryItem, ItemTransaction, Warehouse
from inventory.services.bulk_stock import apply_bulk_stock
from inventory.services.stock_position import refresh_stock_position

LOCATION = "gid://shopify/Location/1"


class FakeShopify:
    """Keeps on_hand per inventory item at LOCATION and answers the stock calls."""

    def __init__(self, levels, untracked=(), stale_writes=0):
        self.levels = dict(levels)  # inventory item gid -> on_hand, or None when not stocked
        self.untracked = set(untracked)
        self.stale_writes = stale_writes
        self.writes = []

    def __call__(self, query, variables=None):
        if "inventorySetQuantities" in query:
            assert "@idempotent(key: $idempotencyKey)" in query and variables["idempotencyKey"]
            self.writes.append(variables)
            if self.stale_writes:
                self.stale_writes -= 1
                return {"inventorySetQuantities": {"userErrors": [{"code": "CHANGE_FROM_QUANTITY_STALE", "field": None, "message": "stale"}]}}
            for change in variables["input"]["quantities"]:
                assert self.levels[change["inventoryItemId"]] == change["changeFromQuantity"]
                self.levels[change["inventoryItemId"]] = change["quantity"]
            return {"inventorySetQuantities": {"userErrors": []}}
        if "inventoryActivate" in query:
            # Required since API 2026-04, like on inventorySetQuantities.
            assert "@idempotent(key: $idempotencyKey)" in query and variables["idempotencyKey"]
            self.levels[variables["inventoryItemId"]] = 0
            return {"inventoryActivate": {"userErrors": []}}
        nodes = []
        for gid in variables["ids"]:
            level = self.levels.get(gid)
            nodes.append({
                "id": gid,
                "tracked": gid not in self.untracked,
                "inventoryLevel": None if level is None else {"quantities": [{"name": "on_hand", "quantity": level}]},
            })
        return {"nodes": nodes}


class StockPushTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.store = ShopifyStore.objects.create(
            company=cls.company, name="Shop", shop_domain="fistex.myshopify.com",
            sync_stock=True, access_token_encrypted="x",
        )
        ShopifyLocationMap.objects.create(store=cls.store, location_id=LOCATION, name="Fistex", warehouse=cls.nave)
        ShopifyLocationMap.objects.create(store=cls.store, location_id="gid://shopify/Location/2", name="Unmapped")
        cls.items = {}
        for n, (code, stock, item_type) in enumerate(
            [("A", 9, ItemType.STOCKABLE), ("B", 5, ItemType.STOCKABLE), ("C", 3, ItemType.STOCKABLE), ("SRV", 0, ItemType.SERVICE)],
            start=1,
        ):
            item = InventoryItem.objects.create(
                company=cls.company, code=code, name=code, item_type=item_type,
                default_warehouse=None if item_type == ItemType.SERVICE else cls.nave,
            )
            if stock:
                ItemTransaction.objects.create(item=item, warehouse=cls.nave, movement_type=MovementType.IN, quantity_change=stock)
            ShopifyVariantLink.objects.create(
                store=cls.store, product_id="p", variant_id=f"v{n}", inventory_item_id=f"inv{n}", sku=code,
                item=item, status=VariantLinkStatus.MATCHED, synced_at="2026-10-20T10:00Z",
            )
            cls.items[code] = item
        InventoryItem.objects.filter(pk=cls.items["A"].pk).update(reserved_quantity=2)

    def push(self, fake, item_ids=None):
        with mock.patch("apps.shopify.client.ShopifyClient.graphql", side_effect=fake):
            return ShopifyStockService.push(self.store, item_ids)

    def test_shopify_shows_sellable_stock_and_unchanged_items_are_not_written(self):
        fake = FakeShopify({"inv1": 1, "inv2": 5, "inv3": None})
        result = self.push(fake)

        self.assertEqual(fake.levels, {"inv1": 7, "inv2": 5, "inv3": 3})  # 9 - 2 reserved; C was stocked first
        self.assertEqual((result.updated, result.unchanged), (2, 1))
        self.assertEqual(len(fake.writes), 1)  # one batched write, unmapped location skipped
        sent = fake.writes[0]
        self.assertTrue(sent["idempotencyKey"])
        self.assertEqual(sent["input"]["name"], "on_hand")
        self.assertNotIn("inv4", str(sent))  # services hold no stock

    def test_untracked_variants_are_reported_not_written(self):
        fake = FakeShopify({"inv1": 0, "inv2": 0, "inv3": 0}, untracked={"inv2"})
        result = self.push(fake)
        self.assertEqual(result.not_tracked, 1)
        self.assertEqual(fake.levels["inv2"], 0)

    def test_stale_read_is_retried_once_then_reported(self):
        fake = FakeShopify({"inv1": 0, "inv2": 0, "inv3": 0}, stale_writes=1)
        self.push(fake)
        self.assertEqual(fake.levels["inv1"], 7)

        with self.assertRaises(ShopifyAPIError):
            self.push(FakeShopify({"inv1": 0, "inv2": 0, "inv3": 0}, stale_writes=2))

    def test_items_only_quoted_stay_at_zero_whatever_their_stock(self):
        ShopifyStore.objects.filter(pk=self.store.pk).update(quote_requests=QuoteRequestMode.ALONGSIDE)
        self.store.refresh_from_db()
        InventoryItem.objects.filter(pk=self.items["B"].pk).update(online_sale_mode=OnlineSaleMode.QUOTE)
        fake = FakeShopify({"inv1": 0, "inv2": 4, "inv3": 0})

        self.push(fake)

        self.assertEqual(fake.levels, {"inv1": 7, "inv2": 0, "inv3": 3})

    def test_push_can_be_limited_to_some_items(self):
        fake = FakeShopify({"inv1": 0, "inv2": 0, "inv3": 0})
        self.push(fake, item_ids=[self.items["B"].pk])
        self.assertEqual(fake.levels, {"inv1": 0, "inv2": 5, "inv3": 0})

    def test_task_pushes_only_to_stores_of_the_items_company(self):
        other_store = ShopifyStore.objects.create(
            company=make_company(name="Other"), name="O", shop_domain="other.myshopify.com",
            sync_stock=True, access_token_encrypted="x",
        )
        with mock.patch("apps.shopify.services.stock.ShopifyStockService.push") as push:
            push_stock_for_items([self.items["A"].pk])
        push.assert_called_once()
        self.assertEqual(push.call_args.args[0], self.store)
        self.assertNotEqual(push.call_args.args[0], other_store)

    def test_one_warehouse_cannot_feed_two_locations(self):
        location = ShopifyLocationMap(store=self.store, location_id="gid://shopify/Location/3", name="Dup", warehouse=self.nave)
        with self.assertRaises(ValidationError):
            location.full_clean()


class StockPushSchedulingTests(TestCase):
    """ERP stock changes queue one push per committed transaction."""

    @classmethod
    def setUpTestData(cls):
        company = make_company(name="Fistex")
        cls.nave = Warehouse.objects.create(company=company, code="NAVE", name="Nave")
        cls.a = InventoryItem.objects.create(company=company, code="A", name="A", default_warehouse=cls.nave)
        cls.b = InventoryItem.objects.create(company=company, code="B", name="B", default_warehouse=cls.nave)
        ShopifyStore.objects.create(
            company=company, name="Shop", shop_domain="fistex.myshopify.com", sync_stock=True, access_token_encrypted="x"
        )

    def setUp(self):
        cache.clear()
        patcher = mock.patch("apps.shopify.tasks.push_stock_for_items.delay")
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def move(self, item, quantity):
        ItemTransaction.objects.create(item=item, warehouse=self.nave, movement_type=MovementType.IN, quantity_change=quantity)

    def test_changes_in_one_transaction_are_pushed_once_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                self.move(self.a, 1)
                self.move(self.b, 2)
                refresh_stock_position([self.a.pk])
                self.delay.assert_not_called()  # nothing leaves before commit
        self.delay.assert_called_once_with(sorted([self.a.pk, self.b.pk]))

    def test_rolled_back_changes_are_not_pushed(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.move(self.a, 1)
                    raise RuntimeError
            except RuntimeError:
                pass
            with transaction.atomic():
                self.move(self.b, 1)
        self.delay.assert_called_once_with([self.b.pk])

    def test_bulk_stock_update_is_pushed(self):
        companies = Company.objects.filter(pk=self.nave.company_id)
        with self.captureOnCommitCallbacks(execute=True):
            plan = apply_bulk_stock("A;4\nB;7\n", companies, user=None)
        self.assertTrue(plan.is_valid)
        self.delay.assert_called_once_with(sorted([self.a.pk, self.b.pk]))

    def test_nothing_is_queued_when_no_store_syncs_stock(self):
        ShopifyStore.objects.update(sync_stock=False)
        cache.clear()
        with self.captureOnCommitCallbacks(execute=True):
            ShopifyPushScheduler.schedule_stock_push([self.a.pk])
        self.delay.assert_not_called()
