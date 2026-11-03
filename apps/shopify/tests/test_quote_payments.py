import copy
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from djmoney.money import Money

from api.fleet.tests.helpers import make_company
from apps.shopify.models import (
    CustomerQuotesMode,
    ShopifyApp,
    ShopifyCustomerLink,
    ShopifyOrder,
    ShopifyQuotePayment,
    ShopifyStore,
    ShopifyVariantLink,
    VariantLinkStatus,
)
from apps.shopify.services.customer_quotes import CustomerQuoteService, CustomerQuoteSummary
from apps.shopify.services.orders import ShopifyOrderService
from apps.shopify.services.quote_payments import QuotePaymentError, ShopifyQuotePaymentService
from apps.shopify.tests.test_orders import BASE_ORDER, ORDER_ID, money
from apps.shopify.tests.test_quote_requests import SECRET, signed_query
from inventory.models import InventoryItem, Warehouse
from invoices.models import SalesInvoice, TaxType
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus

GRAPHQL = "apps.shopify.client.ShopifyClient.graphql"
CUSTOMER = "gid://shopify/Customer/77"
SCOPES = "read_products,read_orders,read_customers,write_draft_orders"


class FakeShopify:
    """Draft orders, and the order a paid draft becomes."""

    def __init__(self, order=None, draft_total=None):
        self.drafts = {}  # gid -> draft
        self.created = []
        self.deleted = []
        self.order = order
        self.draft_total = draft_total  # what Shopify charges, when it differs from the line

    def __call__(self, query, variables=None):
        if "draftOrderCreate" in query:
            self.created.append(variables["input"])
            gid = f"gid://shopify/DraftOrder/{len(self.created)}"
            line = variables["input"]["lineItems"][0]["originalUnitPriceWithCurrency"]
            self.drafts[gid] = {
                "id": gid, "status": "OPEN", "invoiceUrl": f"https://shop.example/invoices/{len(self.created)}",
                "totalPriceSet": money(self.draft_total or line["amount"]), "order": None,
            }
            return {"draftOrderCreate": {"draftOrder": self.drafts[gid], "userErrors": []}}
        if "draftOrderDelete" in query:
            self.deleted.append(variables["input"]["id"])
            self.drafts.pop(variables["input"]["id"], None)
            return {"draftOrderDelete": {"deletedId": variables["input"]["id"], "userErrors": []}}
        if "draftOrder(id" in query:
            return {"draftOrder": self.drafts.get(variables["id"])}
        return {"order": self.order}

    def complete(self, gid, order_id):
        self.drafts[gid].update(status="COMPLETED", order={"id": order_id})


class QuotePaymentTests(TestCase):
    """Paying an accepted quote online: its draft order, and the order it becomes."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        app = ShopifyApp(name="App", client_id="client-1")
        app.set_client_secret(SECRET)
        app.save()
        cls.app = app
        cls.iva = TaxType.objects.create(tax_name="IVA")
        cls.client_ = Client.objects.create(company=cls.company, name="Instalaciones García")
        cls.quote = Quote.objects.create(
            company=cls.company, client=cls.client_, project_name="Acumulador 2000 L",
            status=QuoteStatus.ACCEPTED, total_price=Money("7841.73", "EUR"),
            project_file=ProjectFile.objects.create(name="Acumulador 2000 L", company=cls.company, client=cls.client_),
        )
        nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.tank = InventoryItem.objects.create(company=cls.company, code="T-1", name="Tank", default_warehouse=nave)

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, app=self.app, name="Shop", shop_domain="fistex.myshopify.com",
            access_token_encrypted="x", scopes=SCOPES, import_orders=True, tax_type=self.iva,
            customer_quotes=CustomerQuotesMode.ALL, pay_quotes_online=True,
        )
        ShopifyCustomerLink.objects.create(store=self.store, customer_id=CUSTOMER, client=self.client_, web_store_access=True)
        ShopifyVariantLink.objects.create(
            store=self.store, product_id="p", variant_id="gid://shopify/ProductVariant/1", sku="T-1",
            item=self.tank, status=VariantLinkStatus.MATCHED, synced_at="2026-10-21T08:00Z",
        )
        self.fake = FakeShopify()

    def checkout(self, quote=None):
        with mock.patch(GRAPHQL, side_effect=self.fake):
            return ShopifyQuotePaymentService.checkout_url(self.store, quote or self.fresh_quote(), CUSTOMER)

    def fresh_quote(self):
        return CustomerQuoteService.accepted_quote(self.store, self.client_, self.quote.pk)

    def test_paying_prepares_one_draft_for_the_quotes_exact_total(self):
        url = self.checkout()

        self.assertEqual(url, "https://shop.example/invoices/1")
        sent = self.fake.created[0]
        self.assertEqual(sent["purchasingEntity"], {"customerId": CUSTOMER})
        self.assertEqual(len(sent["lineItems"]), 1)
        line = sent["lineItems"][0]
        self.assertEqual(line["originalUnitPriceWithCurrency"], {"amount": "7841.73", "currencyCode": "EUR"})
        self.assertEqual((line["quantity"], line["taxable"], line["requiresShipping"]), (1, False, False))
        self.assertIn("Acumulador 2000 L", line["title"])
        self.assertFalse(sent["allowDiscountCodesInCheckout"])
        self.assertIn({"key": "polipus_quote", "value": str(self.quote.pk)}, sent["customAttributes"])
        payment = ShopifyQuotePayment.objects.get()
        self.assertEqual((payment.quote, payment.amount, payment.draft_order_id), (self.quote, Decimal("7841.73"), "gid://shopify/DraftOrder/1"))

    def test_the_same_draft_is_reused_until_the_quote_changes(self):
        self.checkout()
        self.assertEqual(self.checkout(), "https://shop.example/invoices/1")
        self.assertEqual(len(self.fake.created), 1)

        Quote.objects.filter(pk=self.quote.pk).update(total_price=Money("7000.00", "EUR"))
        self.assertEqual(self.checkout(), "https://shop.example/invoices/2")
        self.assertEqual(self.fake.deleted, ["gid://shopify/DraftOrder/1"])
        self.assertEqual(ShopifyQuotePayment.objects.get().amount, Decimal("7000.00"))

    def test_a_draft_that_charges_anything_else_is_deleted_and_refused(self):
        self.fake.draft_total = "7900.00"  # e.g. Shopify added tax after all

        with self.assertRaises(QuotePaymentError):
            self.checkout()

        self.assertEqual(self.fake.deleted, ["gid://shopify/DraftOrder/1"])
        self.assertFalse(ShopifyQuotePayment.objects.exists())

    def test_only_accepted_quotes_without_an_order_in_stores_that_take_payments(self):
        quote = self.fresh_quote()
        self.assertTrue(ShopifyQuotePaymentService.can_pay(self.store, quote))

        self.store.scopes = "read_products,read_orders"  # not reconnected since turning it on
        self.assertFalse(ShopifyQuotePaymentService.can_pay(self.store, quote))
        self.store.scopes = SCOPES

        ShopifyOrder.objects.create(store=self.store, order_id="gid://shopify/Order/9", quote=self.quote)
        self.assertFalse(ShopifyQuotePaymentService.can_pay(self.store, self.fresh_quote()))  # e.g. a web order's quote

    def test_only_a_quote_alone_in_its_expediente_with_nothing_invoiced(self):
        # The order is invoiced, and its payment recorded, for the whole expediente.
        other = Quote.objects.create(
            company=self.company, client=self.client_, status=QuoteStatus.ACCEPTED,
            total_price=Money("10.00", "EUR"), project_file=self.quote.project_file,
        )
        self.assertFalse(ShopifyQuotePaymentService.can_pay(self.store, self.fresh_quote()))

        Quote.objects.filter(pk=other.pk).update(status=QuoteStatus.REJECTED)
        self.assertTrue(ShopifyQuotePaymentService.can_pay(self.store, self.fresh_quote()))

        SalesInvoice.objects.create(company=self.company, client=self.client_, project_file=self.quote.project_file)
        self.assertFalse(ShopifyQuotePaymentService.can_pay(self.store, self.fresh_quote()))

    def test_the_quotes_list_costs_the_same_queries_however_many_quotes_can_be_paid(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def summaries():
            return [CustomerQuoteSummary.of(self.store, quote) for quote in CustomerQuoteService.quotes(self.store, self.client_)]

        with CaptureQueriesContext(connection) as one:
            self.assertTrue(summaries()[0].can_pay)
        for name in ("Second", "Third"):
            Quote.objects.create(
                company=self.company, client=self.client_, project_name=name, status=QuoteStatus.ACCEPTED,
                total_price=Money("10.00", "EUR"),
                project_file=ProjectFile.objects.create(name=name, company=self.company, client=self.client_),
            )
        with CaptureQueriesContext(connection) as three:
            self.assertEqual(sum(summary.can_pay for summary in summaries()), 3)

        self.assertEqual(len(three), len(one))

    def test_stock_the_paid_quote_reserves_is_not_counted_as_shopifys(self):
        from apps.shopify.models import ShopifyLocationMap
        from apps.shopify.services.stock import ShopifyStockService
        from inventory.choices import MovementType
        from inventory.models import ItemTransaction
        from sales.models import QuoteInventoryItem

        nave = self.tank.default_warehouse
        ItemTransaction.objects.create(item=self.tank, warehouse=nave, movement_type=MovementType.IN, quantity_change=10)
        QuoteInventoryItem.objects.create(quote=self.quote, inventory_item=self.tank, quantity=8)
        InventoryItem.objects.filter(pk=self.tank.pk).update(reserved_quantity=8)
        ShopifyVariantLink.objects.filter(item=self.tank).update(inventory_item_id="gid://shopify/InventoryItem/1")
        ShopifyLocationMap.objects.create(store=self.store, location_id="gid://shopify/Location/1", name="Nave", warehouse=nave)
        self.store.sync_stock = True
        self.store.save()
        order = ShopifyOrder.objects.create(store=self.store, order_id=ORDER_ID, quote=self.quote)
        ShopifyQuotePayment.objects.create(
            store=self.store, quote=self.quote, draft_order_id="d", checkout_url="https://shop.example/i",
            amount=Decimal("7841.73"), currency="EUR", customer_id=CUSTOMER, order=order,
        )
        sent = []

        def fake(query, variables=None):
            if "inventorySetQuantities" in query:
                sent.extend(variables["input"]["quantities"])
                return {"inventorySetQuantities": {"userErrors": []}}
            return {"nodes": [{"id": gid, "tracked": True, "inventoryLevel": {"quantities": [{"name": "on_hand", "quantity": 0}]}}
                              for gid in variables["ids"]]}

        with mock.patch(GRAPHQL, side_effect=fake):
            ShopifyStockService.push(self.store)

        # Shopify reserved nothing for the draft order's custom line: 10 on hand, 8 for the quote.
        self.assertEqual([change["quantity"] for change in sent], [2])

    def test_taking_payments_needs_the_quotes_area_and_order_import(self):
        self.store.customer_quotes = CustomerQuotesMode.OFF
        with self.assertRaises(ValidationError):
            self.store.full_clean()
        self.store.customer_quotes = CustomerQuotesMode.ALL
        self.store.import_orders = False
        with self.assertRaises(ValidationError):
            self.store.full_clean()

    def test_the_pay_endpoint_sends_the_customer_to_checkout_and_refuses_others(self):
        def pay(quote_id):
            url = reverse("shopify_customer_quote_pay", args=[quote_id])
            with mock.patch(GRAPHQL, side_effect=self.fake):
                return self.client.post(f"{url}?{signed_query(logged_in_customer_id='77')}")

        response = pay(self.quote.pk)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["url"], "https://shop.example/invoices/1")

        other = Quote.objects.create(
            company=self.company, client=Client.objects.create(company=self.company, name="Otro"),
            status=QuoteStatus.ACCEPTED, total_price=Money("100.00", "EUR"),
        )
        self.assertEqual(pay(other.pk).status_code, 404)
        sent = Quote.objects.create(company=self.company, client=self.client_, status=QuoteStatus.SENT, total_price=Money("5.00", "EUR"))
        self.assertEqual(pay(sent.pk).status_code, 404)  # accept it first

    def paid_order(self, **changes):
        data = copy.deepcopy(BASE_ORDER)
        data.update(
            currentTotalPriceSet=money("7841.73"), totalPriceSet=money("7841.73"), shippingLines={"nodes": []},
            customAttributes=[{"key": "polipus_quote", "value": str(self.quote.pk)}],
            lineItems={"nodes": [{
                "id": "gid://shopify/LineItem/1", "name": "Presupuesto", "quantity": 1, "currentQuantity": 1,
                "variant": None, "customAttributes": [], "originalUnitPriceSet": money("7841.73"),
                "discountAllocations": [], "taxLines": [],
            }]},
        )
        data.update(changes)
        return data

    def test_a_refund_of_a_paid_quote_is_kept_for_crediting_or_review(self):
        from apps.shopify.models import RefundStatus

        self.checkout()
        refund = {"id": "gid://shopify/Refund/1", "createdAt": "2026-10-22T09:00:00Z", "totalRefundedSet": money("100.00"),
                  "refundLineItems": {"nodes": []}, "refundShippingLines": {"nodes": []}}
        self.fake.order = self.paid_order(refunds=[refund], totalRefundedSet=money("100.00"))
        self.fake.complete("gid://shopify/DraftOrder/1", ORDER_ID)

        with mock.patch(GRAPHQL, side_effect=self.fake):
            record = ShopifyOrderService.sync_order(self.store, ORDER_ID)

        # The quote is never rebuilt from the order, so nothing settles the refund on it.
        self.assertNotEqual(record.refunds.get().status, RefundStatus.NO_CREDIT)

    def test_the_paid_draft_is_filed_under_its_quote(self):
        self.checkout()
        self.fake.order = self.paid_order()
        self.fake.complete("gid://shopify/DraftOrder/1", ORDER_ID)

        with mock.patch(GRAPHQL, side_effect=self.fake):
            record = ShopifyOrderService.sync_order(self.store, ORDER_ID)

        self.assertEqual(record.quote, self.quote)  # no new quote from its line
        self.assertEqual(Quote.objects.count(), 1)
        self.assertTrue(record.totals_match)
        payment = ShopifyQuotePayment.objects.get()
        self.assertEqual(payment.order, record)
        self.assertTrue(payment.is_paid)
        summary = CustomerQuoteSummary.of(self.store, self.fresh_quote())
        self.assertEqual((summary.state, summary.can_pay), ("paid", False))

    def test_an_order_that_only_names_a_quote_is_imported_as_any_other(self):
        self.checkout()
        # Someone added the attribute to an ordinary cart: the draft is still unpaid.
        self.fake.order = copy.deepcopy(BASE_ORDER) | {"customAttributes": [{"key": "polipus_quote", "value": str(self.quote.pk)}]}
        self.fake.order["lineItems"]["nodes"][0]["customAttributes"] = []
        self.fake.order["shippingLines"] = {"nodes": []}

        with mock.patch(GRAPHQL, side_effect=self.fake):
            record = ShopifyOrderService.sync_order(self.store, ORDER_ID)

        self.assertNotEqual(record.quote, self.quote)
        self.assertIsNone(ShopifyQuotePayment.objects.get().order)
        self.assertEqual(Quote.objects.get(pk=self.quote.pk).total_price.amount, Decimal("7841.73"))
