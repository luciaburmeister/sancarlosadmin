import copy
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from api.fleet.tests.helpers import make_company
from apps.shopify.models import (
    ClientMode,
    ShopifyCustomerLink,
    ShopifyOrder,
    ShopifyStore,
    ShopifyVariantLink,
    ShopifyWebhookEvent,
    VariantLinkStatus,
    WebhookEventStatus,
)
from apps.shopify.services.orders import ShopifyOrderError, ShopifyOrderService
from apps.shopify.services.webhooks import ShopifyWebhookService
from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction, Warehouse
from invoices.models import TaxType
from sales.models import Client, QuoteStatus

GRAPHQL = "apps.shopify.client.ShopifyClient.graphql"
ORDER_ID = "gid://shopify/Order/1"


def money(amount):
    return {"shopMoney": {"amount": amount, "currencyCode": "EUR"}}


BASE_ORDER = {
    "id": ORDER_ID,
    "name": "#1001",
    "createdAt": "2026-10-21T09:00:00Z",
    "updatedAt": "2026-10-21T09:00:00Z",
    "cancelledAt": None,
    "displayFinancialStatus": "PAID",
    "taxesIncluded": True,
    "email": "ana@example.com",
    "phone": None,
    "currentTotalPriceSet": money("235.95"),
    "totalPriceSet": money("235.95"),
    "totalRefundedSet": money("0.00"),
    "customer": {"id": "gid://shopify/Customer/7", "email": "ana@example.com", "firstName": "Ana", "lastName": "García", "phone": None},
    "billingAddress": {"name": "Ana García", "company": None, "address1": "Calle Mayor 1", "address2": None,
                       "city": "Madrid", "zip": "28001", "province": "Madrid", "country": "Spain", "phone": None},
    "shippingAddress": None,
    "lineItems": {"nodes": [{
        # 2 × 121.00 incl. VAT, 12.10 discount: 229.90 gross = 190.00 net + 39.90 VAT
        "id": "gid://shopify/LineItem/1",
        "name": "Tank 100 L", "quantity": 2, "currentQuantity": 2, "variant": {"id": "gid://shopify/ProductVariant/1"},
        "originalUnitPriceSet": money("121.00"),
        "discountAllocations": [{"allocatedAmountSet": money("12.10")}],
        "taxLines": [{"ratePercentage": 21.0, "priceSet": money("39.90")}],
    }]},
    "shippingLines": {"nodes": [{
        "originalPriceSet": money("6.05"), "discountAllocations": [],
        "taxLines": [{"ratePercentage": 21.0, "priceSet": money("1.05")}],
    }]},
    "refunds": [],
}


def order(**changes):
    data = copy.deepcopy(BASE_ORDER)
    data.update(changes)
    return {"order": data}


class OrderImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.tank = InventoryItem.objects.create(company=cls.company, code="T-1", name="Tank", default_warehouse=cls.nave)
        ItemTransaction.objects.create(item=cls.tank, warehouse=cls.nave, movement_type=MovementType.IN, quantity_change=10)
        cls.portes = InventoryItem.objects.create(company=cls.company, code="SRV-1", name="Portes", item_type=ItemType.SERVICE)
        cls.iva = TaxType.objects.create(tax_name="IVA")

    def setUp(self):
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            import_orders=True, tax_type=self.iva, shipping_item=self.portes,
            scopes="read_products,read_orders,read_customers",
        )
        ShopifyVariantLink.objects.create(
            store=self.store, product_id="p", variant_id="gid://shopify/ProductVariant/1", sku="T-1",
            item=self.tank, status=VariantLinkStatus.MATCHED, synced_at="2026-10-21T08:00Z",
        )

    def sync(self, payload):
        with mock.patch(GRAPHQL, return_value=payload):
            return ShopifyOrderService.sync_order(self.store, ORDER_ID)

    def test_paid_order_becomes_an_accepted_quote_with_shopifys_exact_amounts(self):
        record = self.sync(order())
        quote = record.quote

        self.assertEqual((quote.status, quote.external_id, quote.prices_from_channel), (QuoteStatus.ACCEPTED, ORDER_ID, True))
        lines = {line.inventory_item: line for line in quote.inventory_items.all()}
        self.assertEqual((lines[self.tank].quantity, lines[self.tank].line_total.amount), (Decimal("2"), Decimal("190.00")))
        self.assertEqual(lines[self.portes].line_total.amount, Decimal("5.00"))
        self.assertEqual((quote.tax_amount.amount, quote.total_price.amount), (Decimal("40.95"), Decimal("235.95")))
        self.assertTrue(record.totals_match)
        self.assertEqual((quote.client.name, quote.client.email), ("Ana García", "ana@example.com"))

    def test_repeated_events_change_nothing_and_reuse_the_customer(self):
        first = self.sync(order())
        again = self.sync(order())
        self.assertEqual(again.quote, first.quote)
        self.assertEqual(ShopifyOrder.objects.count(), 1)

        self.sync(order(id="gid://shopify/Order/2", name="#1002"))
        self.assertEqual(Client.objects.filter(company=self.company).count(), 1)
        self.assertEqual(ShopifyCustomerLink.objects.get().client, first.quote.client)

    def test_unpaid_order_reserves_nothing_until_paid(self):
        record = self.sync(order(displayFinancialStatus="PENDING"))
        self.assertEqual(record.quote.status, QuoteStatus.SENT)
        record = self.sync(order(displayFinancialStatus="PAID", updatedAt="2026-10-21T10:00:00Z"))
        self.assertEqual(record.quote.status, QuoteStatus.ACCEPTED)

    def test_cancelled_order_rejects_its_quote(self):
        self.sync(order())
        record = self.sync(order(cancelledAt="2026-10-21T11:00:00Z", updatedAt="2026-10-21T11:00:00Z"))
        self.assertTrue(record.is_cancelled)
        self.assertEqual(record.quote.status, QuoteStatus.REJECTED)

    def test_cent_difference_is_imported_and_flagged(self):
        record = self.sync(order(currentTotalPriceSet=money("235.96")))
        self.assertFalse(record.totals_match)
        self.assertEqual((record.shopify_total, record.erp_total), (Decimal("235.96"), Decimal("235.95")))

    def test_unlinked_product_holds_the_order_with_a_reason(self):
        data = order()
        data["order"]["lineItems"]["nodes"][0]["variant"]["id"] = "gid://shopify/ProductVariant/999"
        event = ShopifyWebhookEvent.objects.create(
            store=self.store, webhook_id="wh-1", topic="orders/create", payload={"admin_graphql_api_id": ORDER_ID}
        )
        with mock.patch(GRAPHQL, return_value=data):
            event = ShopifyWebhookService.process(event.pk)

        self.assertEqual(event.status, WebhookEventStatus.FAILED)
        self.assertIn("Tank 100 L", event.error)
        record = ShopifyOrder.objects.get()
        self.assertIsNone(record.quote)
        self.assertIn("Tank 100 L", record.error)

    def test_store_without_order_import_ignores_order_events(self):
        self.store.import_orders = False
        self.store.save()
        event = ShopifyWebhookEvent.objects.create(
            store=self.store, webhook_id="wh-1", topic="orders/create", payload={"admin_graphql_api_id": ORDER_ID}
        )
        with mock.patch(GRAPHQL) as graphql:
            event = ShopifyWebhookService.process(event.pk)
        graphql.assert_not_called()
        self.assertEqual(event.status, WebhookEventStatus.IGNORED)

    def test_without_customer_access_the_client_comes_from_the_order(self):
        self.store.scopes = "read_products,read_orders"
        self.store.save()
        data = order()
        del data["order"]["customer"]  # Shopify wasn't asked for it
        with mock.patch(GRAPHQL, return_value=data) as graphql:
            record = ShopifyOrderService.sync_order(self.store, ORDER_ID)

        self.assertNotIn("customer {", graphql.call_args.args[0])
        self.assertEqual((record.quote.client.name, record.quote.client.email), ("Ana García", "ana@example.com"))
        self.assertFalse(ShopifyCustomerLink.objects.exists())

    def test_single_client_mode_uses_the_web_client(self):
        web_client = Client.objects.create(company=self.company, name="Ventas web")
        self.store.client_mode, self.store.web_client = ClientMode.SINGLE_CLIENT, web_client
        self.store.save()
        self.assertEqual(self.sync(order()).quote.client, web_client)

    def test_shipping_needs_a_shipping_item(self):
        self.store.shipping_item = None
        self.store.save()
        with self.assertRaises(ShopifyOrderError):
            self.sync(order())

    def test_stock_push_does_not_count_the_stores_own_orders_twice(self):
        self.sync(order())  # accepted: reserves 2 tanks in the ERP
        own_quotes = self.store.orders.values("quote_id")
        items = InventoryItem.objects.filter(pk=self.tank.pk)
        self.assertEqual(items.with_sellable_quantity(self.nave).get().sellable_quantity, 8)
        # Shopify already holds back its own order's 2 tanks.
        self.assertEqual(items.with_sellable_quantity(self.nave, channel_quote_ids=own_quotes).get().sellable_quantity, 10)

    def test_periodic_check_imports_missed_orders_and_moves_on(self):
        self.store.orders_synced_at = datetime(2026, 10, 21, 8, tzinfo=dt_timezone.utc)
        self.store.save()
        pages = {
            None: {"orders": {"nodes": [{"id": ORDER_ID, "updatedAt": "2026-10-21T09:00:00Z"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}},
            "c1": {"orders": {"nodes": [{"id": "gid://shopify/Order/2", "updatedAt": "2026-10-21T09:30:00Z"}], "pageInfo": {"hasNextPage": False, "endCursor": None}}},
        }
        broken = order(id="gid://shopify/Order/2", name="#1002")
        broken["order"]["lineItems"]["nodes"][0]["variant"] = None

        def graphql(query, variables=None):
            if "orders(" in query:
                return pages[variables["after"]]
            return order() if variables["id"] == ORDER_ID else broken

        with mock.patch(GRAPHQL, side_effect=graphql):
            self.assertEqual(ShopifyOrderService.sync_updated_orders(self.store), 2)

        self.assertIsNotNone(ShopifyOrder.objects.get(order_id=ORDER_ID).quote)
        self.assertTrue(ShopifyOrder.objects.get(order_id="gid://shopify/Order/2").error)
        self.store.refresh_from_db()
        self.assertEqual(self.store.orders_synced_at, datetime(2026, 10, 21, 9, 30, tzinfo=dt_timezone.utc))

    def test_missing_phone_and_address_are_filled_in_and_set_details_are_kept(self):
        client = Client.objects.create(company=self.company, name="Ana García SL", email="ana@example.com", phone="")
        data = order()
        data["order"]["customer"]["phone"] = "+34600999888"
        record = self.sync(data)

        client.refresh_from_db()
        self.assertEqual(record.quote.client, client)
        self.assertEqual((client.name, client.phone), ("Ana García SL", "+34600999888"))
        self.assertEqual(client.addresses.get().street, "Calle Mayor 1")

        # The next order with other details changes nothing staff already have.
        later = order(id="gid://shopify/Order/2", name="#1002")
        later["order"]["customer"]["phone"] = "+34611000000"
        later["order"]["billingAddress"] = {**later["order"]["billingAddress"], "address1": "Otra calle 3"}
        self.sync(later)
        client.refresh_from_db()
        self.assertEqual(client.phone, "+34600999888")
        self.assertEqual(list(client.addresses.values_list("street", flat=True)), ["Calle Mayor 1"])


class StoreOrderSettingsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.foreign_client = Client.objects.create(company=make_company(name="Other"), name="Foreign")

    def store(self, **fields):
        return ShopifyStore(company=self.company, name="Shop", shop_domain="fistex.myshopify.com", **fields)

    def test_importing_needs_a_tax_type_and_own_company_settings(self):
        with self.assertRaises(ValidationError) as ctx:
            self.store(import_orders=True, client_mode=ClientMode.SINGLE_CLIENT, web_client=self.foreign_client).full_clean()
        self.assertLessEqual({"tax_type", "web_client"}, set(ctx.exception.message_dict))

    def test_turning_import_on_starts_the_check_from_now(self):
        store = self.store()
        store.save()
        self.assertIsNone(store.orders_synced_at)
        store.import_orders = True
        store.save(update_fields=["import_orders"])
        store.refresh_from_db()
        self.assertIsNotNone(store.orders_synced_at)
