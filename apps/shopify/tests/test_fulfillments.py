from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase

from api.fleet.tests.helpers import make_company
from apps.shopify.exceptions import ShopifyError
from apps.shopify.models import (
    FulfillmentBy,
    FulfillmentCancellation,
    ShopifyFulfillment,
    ShopifyOrder,
    ShopifyStore,
    ShopifyVariantLink,
    VariantLinkStatus,
)
from apps.shopify.services.fulfillments import ShopifyFulfillmentService
from apps.shopify.services.orders import ShopifyOrderService
from apps.shopify.tests.test_orders import ORDER_ID, order
from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction, Warehouse
from invoices.models import TaxType
from sales.models import DeliveryOrder, DeliveryOrderStatus, QuoteStatus, ShippingAgency
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService

GRAPHQL = "apps.shopify.client.ShopifyClient.graphql"
VARIANT = "gid://shopify/ProductVariant/1"


class FakeFulfillments:
    """Answers the fulfillment order read and records fulfillmentCreate calls."""

    def __init__(self, remaining=2, errors=()):
        self.remaining = remaining
        self.errors = list(errors)
        self.created = []

    def __call__(self, query, variables=None):
        if "fulfillmentCreate" in query:
            self.created.append(variables["fulfillment"])
            return {"fulfillmentCreate": {"fulfillment": {"id": f"gid://shopify/Fulfillment/{len(self.created)}"}, "userErrors": self.errors}}
        return {"order": {"fulfillmentOrders": {"nodes": [
            {"id": "gid://shopify/FulfillmentOrder/9", "status": "CLOSED", "lineItems": {"nodes": []}},
            {"id": "gid://shopify/FulfillmentOrder/10", "status": "OPEN", "lineItems": {"nodes": [
                {"id": "gid://shopify/FulfillmentOrderLineItem/5", "remainingQuantity": self.remaining, "lineItem": {"variant": {"id": VARIANT}}},
            ]}},
        ]}}}


class FulfillmentTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.tank = InventoryItem.objects.create(company=cls.company, code="T-1", name="Tank", default_warehouse=cls.nave)
        ItemTransaction.objects.create(item=cls.tank, warehouse=cls.nave, movement_type=MovementType.IN, quantity_change=10)
        cls.portes = InventoryItem.objects.create(company=cls.company, code="SRV-1", name="Portes", item_type=ItemType.SERVICE)
        cls.iva = TaxType.objects.create(tax_name="IVA")
        cls.seur = ShippingAgency.objects.create(company=cls.company, name="SEUR", tracking_url_template="https://seur.example/{shipment_number}")

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            import_orders=True, tax_type=self.iva, shipping_item=self.portes, fulfillment_by=self.fulfillment_by,
            scopes="read_orders,read_customers",
        )
        ShopifyVariantLink.objects.create(
            store=self.store, product_id="p", variant_id=VARIANT, sku="T-1",
            item=self.tank, status=VariantLinkStatus.MATCHED, synced_at="2026-10-21T08:00Z",
        )

    def sync(self, payload):
        with mock.patch(GRAPHQL, return_value=payload):
            return ShopifyOrderService.sync_order(self.store, ORDER_ID)

    def stock_out(self):
        return -sum(ItemTransaction.objects.filter(item=self.tank, quantity_change__lt=0).values_list("quantity_change", flat=True))

    def on_hand(self):
        return sum(ItemTransaction.objects.filter(item=self.tank).values_list("quantity_change", flat=True))


class ErpShipsTests(FulfillmentTestBase):
    fulfillment_by = FulfillmentBy.ERP

    def pending(self):
        return DeliveryOrder.objects.get(external_id=ORDER_ID, status=DeliveryOrderStatus.DRAFT)

    def dispatch(self, delivery_order):
        DeliveryOrderFulfillmentService.dispatch(delivery_order, shipping_agency=self.seur, shipment_number="ABC123")

    def test_paid_order_gets_a_delivery_order_pending_shipment(self):
        self.sync(order(displayFinancialStatus="PENDING"))
        self.assertFalse(DeliveryOrder.objects.exists())  # nothing to ship until paid

        self.sync(order(updatedAt="2026-10-21T10:00:00Z"))
        delivery_order = self.pending()
        self.assertEqual(
            {(line.inventory_item, line.quantity) for line in delivery_order.items.all()},
            {(self.tank, Decimal("2")), (self.portes, Decimal("1"))},
        )
        self.assertEqual(delivery_order.total_amount.amount, Decimal("235.95"))

        self.sync(order(updatedAt="2026-10-21T11:00:00Z"))
        self.assertEqual(DeliveryOrder.objects.count(), 1)  # never a second one

    def test_edits_and_cancellation_follow_while_still_pending(self):
        self.sync(order())
        first = self.pending()
        edited = order(updatedAt="2026-10-21T10:00:00Z")
        edited["order"]["lineItems"]["nodes"][0]["currentQuantity"] = 1
        self.sync(edited)

        first.refresh_from_db()
        self.assertEqual(first.status, DeliveryOrderStatus.CANCELLED)
        self.assertEqual(self.pending().items.get(inventory_item=self.tank).quantity, Decimal("1"))

        record = self.sync(order(cancelledAt="2026-10-21T11:00:00Z", updatedAt="2026-10-21T11:00:00Z"))
        self.assertEqual(record.quote.status, QuoteStatus.REJECTED)
        self.assertFalse(DeliveryOrder.objects.exclude(status=DeliveryOrderStatus.CANCELLED).exists())

    def test_dispatching_queues_the_shopify_push_after_commit(self):
        self.sync(order())
        delivery_order = self.pending()
        with mock.patch("apps.shopify.tasks.push_fulfillment.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.dispatch(delivery_order)
        delay.assert_called_once_with(delivery_order.pk)
        self.assertEqual(self.stock_out(), 2)

    def test_dispatched_order_is_fulfilled_in_shopify_with_its_tracking(self):
        self.sync(order())
        delivery_order = self.pending()
        self.dispatch(delivery_order)
        fake = FakeFulfillments()

        with mock.patch(GRAPHQL, side_effect=fake):
            fulfillment = ShopifyFulfillmentService.push_delivery_order(delivery_order.pk)
            ShopifyFulfillmentService.push_delivery_order(delivery_order.pk)  # repeated: nothing new

        self.assertEqual(len(fake.created), 1)
        sent = fake.created[0]
        self.assertEqual(sent["lineItemsByFulfillmentOrder"], [{
            "fulfillmentOrderId": "gid://shopify/FulfillmentOrder/10",
            "fulfillmentOrderLineItems": [{"id": "gid://shopify/FulfillmentOrderLineItem/5", "quantity": 2}],
        }])
        self.assertEqual(sent["trackingInfo"], {"company": "SEUR", "number": "ABC123", "url": "https://seur.example/ABC123"})
        self.assertTrue(sent["notifyCustomer"])
        self.assertEqual(fulfillment.fulfillment_id, "gid://shopify/Fulfillment/1")

        # The orders/updated that follows doesn't create anything new.
        self.sync(order(updatedAt="2026-10-21T12:00:00Z"))
        self.assertEqual(DeliveryOrder.objects.count(), 1)

    def test_mismatch_with_shopify_is_recorded_and_retried_later(self):
        self.sync(order())
        delivery_order = self.pending()
        self.dispatch(delivery_order)

        with mock.patch(GRAPHQL, side_effect=FakeFulfillments(remaining=1)), self.assertRaises(ShopifyError):
            ShopifyFulfillmentService.push_delivery_order(delivery_order.pk)
        self.assertIn(delivery_order.number, ShopifyFulfillment.objects.get().error)

        # The periodic check (or the admin action) sends it once Shopify agrees.
        with mock.patch(GRAPHQL, side_effect=FakeFulfillments()):
            self.assertEqual(ShopifyFulfillmentService.push_pending(self.store), (1, 0))
        fulfillment = ShopifyFulfillment.objects.get()
        self.assertEqual((fulfillment.fulfillment_id, fulfillment.error), ("gid://shopify/Fulfillment/1", ""))


class ShopifyShipsTests(FulfillmentTestBase):
    fulfillment_by = FulfillmentBy.SHOPIFY

    def fulfilled(self, quantity=2, fulfillment_id="gid://shopify/Fulfillment/77", status="SUCCESS", updated="2026-10-21T10:00:00Z"):
        data = order(updatedAt=updated)
        data["order"]["fulfillments"] = [{
            "id": fulfillment_id, "status": status,
            "trackingInfo": [{"company": "seur", "number": "XYZ9", "url": "https://seur.example/XYZ9"}],
            "fulfillmentLineItems": {"nodes": [{"quantity": quantity, "lineItem": {"variant": {"id": VARIANT}}}]},
        }]
        return data

    def test_shipment_in_shopify_becomes_a_dispatched_delivery_order(self):
        self.sync(order())
        self.assertFalse(DeliveryOrder.objects.exists())  # Shopify ships: nothing pending in the ERP

        record = self.sync(self.fulfilled())
        delivery_order = DeliveryOrder.objects.get()
        self.assertEqual(delivery_order.status, DeliveryOrderStatus.DISPATCHED)
        self.assertEqual(delivery_order.project_file, record.quote.project_file)
        self.assertEqual((delivery_order.shipping_agency, delivery_order.shipment_number), (self.seur, "XYZ9"))
        self.assertEqual(
            {(line.inventory_item, line.quantity) for line in delivery_order.items.all()},
            {(self.tank, Decimal("2")), (self.portes, Decimal("1"))},  # shipping costs go with the first shipment
        )
        self.assertEqual(self.stock_out(), 2)

        self.sync(self.fulfilled(updated="2026-10-21T11:00:00Z"))
        self.assertEqual(DeliveryOrder.objects.count(), 1)

    def test_split_shipments_and_unsuccessful_ones(self):
        self.sync(self.fulfilled(quantity=1, fulfillment_id="gid://shopify/Fulfillment/1"))
        data = self.fulfilled(quantity=1, fulfillment_id="gid://shopify/Fulfillment/2", updated="2026-10-21T11:00:00Z")
        data["order"]["fulfillments"].append({**data["order"]["fulfillments"][0], "id": "gid://shopify/Fulfillment/3", "status": "CANCELLED"})
        data["order"]["fulfillments"].insert(0, self.fulfilled(quantity=1, fulfillment_id="gid://shopify/Fulfillment/1")["order"]["fulfillments"][0])
        self.sync(data)

        self.assertEqual(DeliveryOrder.objects.count(), 2)
        self.assertEqual(self.stock_out(), 2)
        self.assertEqual(ShopifyFulfillment.objects.count(), 2)

    def test_erp_never_pushes_for_a_store_that_ships_in_shopify(self):
        self.sync(self.fulfilled())
        with mock.patch(GRAPHQL) as graphql:
            self.assertIsNone(ShopifyFulfillmentService.push_delivery_order(DeliveryOrder.objects.get().pk))
        graphql.assert_not_called()


class CancelledInShopifyTests(ShopifyShipsTests):
    """A shipment cancelled in Shopify, where Shopify ships."""

    FIRST = "gid://shopify/Fulfillment/77"
    SECOND = "gid://shopify/Fulfillment/78"

    def ship_then_cancel(self):
        self.sync(self.fulfilled())
        record = self.sync(self.fulfilled(status="CANCELLED", updated="2026-10-21T11:00:00Z"))
        return record, record.fulfillments.get(fulfillment_id=self.FIRST)

    def ship_again(self, updated="2026-10-21T12:00:00Z"):
        data = self.fulfilled(status="CANCELLED", updated=updated)
        data["order"]["fulfillments"].append(self.fulfilled(fulfillment_id=self.SECOND)["order"]["fulfillments"][0])
        return self.sync(data)

    def test_a_cancelled_shipment_is_flagged_and_nothing_moves_by_itself(self):
        record, fulfillment = self.ship_then_cancel()
        self.assertEqual(fulfillment.cancellation, FulfillmentCancellation.REVIEW)
        self.assertEqual(fulfillment.delivery_order.status, DeliveryOrderStatus.DISPATCHED)
        self.assertEqual(self.on_hand(), 8)  # the goods may be lost: stock stays out until staff say
        self.assertTrue(ShopifyOrder.objects.filter(pk=record.pk, fulfillments__cancellation=FulfillmentCancellation.REVIEW).exists())

    def test_goods_back_return_the_stock_and_the_order_ships_normally_again(self):
        _record, fulfillment = self.ship_then_cancel()
        ShopifyFulfillmentService.goods_returned(fulfillment)

        fulfillment.refresh_from_db()
        self.assertEqual(fulfillment.cancellation, FulfillmentCancellation.RETURNED)
        self.assertEqual(fulfillment.delivery_order.status, DeliveryOrderStatus.CANCELLED)
        self.assertEqual(self.on_hand(), 10)

        self.ship_again()
        shipped = DeliveryOrder.objects.get(status=DeliveryOrderStatus.DISPATCHED)
        self.assertIsNone(shipped.replaces)
        self.assertEqual(shipped.total_amount.amount, Decimal("235.95"))
        self.assertEqual(self.on_hand(), 8)

    def test_lost_goods_stay_out_and_ship_again_free_of_charge(self):
        _record, fulfillment = self.ship_then_cancel()
        ShopifyFulfillmentService.goods_lost(fulfillment)
        self.assertEqual(DeliveryOrder.objects.count(), 1)  # Shopify ships: the replacement comes with its shipment

        record = self.ship_again()
        self.assertEqual(record.error, "")
        replacement = DeliveryOrder.objects.get(replaces=fulfillment.delivery_order)
        self.assertEqual(replacement.status, DeliveryOrderStatus.DISPATCHED)
        self.assertEqual(replacement.total_amount.amount, Decimal("0.00"))
        self.assertEqual(list(replacement.items.values_list("inventory_item", "quantity")), [(self.tank.pk, Decimal("2"))])
        self.assertEqual(self.on_hand(), 6)  # the lost two and the two sent again
        self.assertEqual(record.fulfillments.get(fulfillment_id=self.SECOND).delivery_order, replacement)

    def test_staff_can_only_resolve_a_cancellation_once(self):
        _record, fulfillment = self.ship_then_cancel()
        ShopifyFulfillmentService.goods_lost(fulfillment)
        with self.assertRaises(ValidationError):
            ShopifyFulfillmentService.goods_returned(fulfillment)

    def test_a_shipment_invoiced_on_its_own_must_be_credited_before_its_goods_come_back(self):
        _record, fulfillment = self.ship_then_cancel()
        from sales.services.external_order import ExternalOrderService

        invoice = ExternalOrderService.invoice_delivery_order(fulfillment.delivery_order)
        with self.assertRaisesMessage(ValidationError, invoice.display_number):
            ShopifyFulfillmentService.goods_returned(fulfillment)
        self.assertEqual(self.on_hand(), 8)


class ErpShipmentCancelledInShopifyTests(FulfillmentTestBase):
    """A shipment the ERP sent, then cancelled in Shopify."""

    fulfillment_by = FulfillmentBy.ERP

    def shipped_then_cancelled(self):
        self.sync(order())
        delivery_order = DeliveryOrder.objects.get()
        DeliveryOrderFulfillmentService.dispatch(delivery_order)
        with mock.patch(GRAPHQL, side_effect=FakeFulfillments()):
            fulfillment = ShopifyFulfillmentService.push_delivery_order(delivery_order.pk)
        data = order(updatedAt="2026-10-21T11:00:00Z")
        data["order"]["fulfillments"] = [{
            "id": fulfillment.fulfillment_id, "status": "CANCELLED", "trackingInfo": [],
            "fulfillmentLineItems": {"nodes": [{"quantity": 2, "lineItem": {"variant": {"id": VARIANT}}}]},
        }]
        self.sync(data)
        fulfillment.refresh_from_db()
        return fulfillment

    def test_goods_back_put_the_order_back_on_a_pending_delivery_order(self):
        fulfillment = self.shipped_then_cancelled()
        self.assertEqual(fulfillment.cancellation, FulfillmentCancellation.REVIEW)
        ShopifyFulfillmentService.goods_returned(fulfillment)

        self.assertEqual(self.on_hand(), 10)
        pending = DeliveryOrder.objects.get(status=DeliveryOrderStatus.DRAFT)
        self.assertEqual(pending.items.get(inventory_item=self.tank).quantity, Decimal("2"))

    def test_lost_goods_get_a_replacement_pending_shipment_that_is_sent_to_shopify(self):
        fulfillment = self.shipped_then_cancelled()
        ShopifyFulfillmentService.goods_lost(fulfillment)

        replacement = DeliveryOrder.objects.get(replaces=fulfillment.delivery_order)
        self.assertEqual((replacement.status, replacement.total_amount.amount), (DeliveryOrderStatus.DRAFT, Decimal("0.00")))
        DeliveryOrderFulfillmentService.dispatch(replacement)
        fake = FakeFulfillments()
        fake.created.append(None)  # Shopify gives the new shipment a new id
        with mock.patch(GRAPHQL, side_effect=fake):
            pushed = ShopifyFulfillmentService.push_delivery_order(replacement.pk)
        self.assertEqual(pushed.order, fulfillment.order)
        self.assertEqual(fake.created[1]["lineItemsByFulfillmentOrder"][0]["fulfillmentOrderLineItems"][0]["quantity"], 2)
        self.assertEqual(self.on_hand(), 6)


class ReplacementDeliveryOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.tank = InventoryItem.objects.create(company=cls.company, code="T-1", name="Tank", default_warehouse=nave)
        ItemTransaction.objects.create(item=cls.tank, warehouse=nave, movement_type=MovementType.IN, quantity_change=10)
        from sales.models import Client

        cls.client_ = Client.objects.create(company=cls.company, name="Ana")

    def test_replacements_cover_at_most_what_was_lost_and_are_never_invoiced(self):
        from invoices.services.delivery_order_service import DeliveryOrderService
        from django.utils import timezone

        original = DeliveryOrder.objects.create(company=self.company, client=self.client_, number="DO-1")
        from sales.models import DeliveryOrderItem

        DeliveryOrderItem.objects.create(delivery_order=original, inventory_item=self.tank, quantity=3, unit_price=50)
        first = DeliveryOrderService.create_replacement(original, quantities={self.tank.pk: 2})
        self.assertEqual(DeliveryOrderService.replaceable_quantities(original), {self.tank.pk: Decimal("1")})
        with self.assertRaises(ValidationError):
            DeliveryOrderService.create_replacement(original, quantities={self.tank.pk: 2})
        with self.assertRaisesMessage(ValidationError, first.number):
            DeliveryOrderService.create_invoice_from_delivery_orders([first], self.company, self.client_, timezone.localdate())


class ShippingAddressTests(FulfillmentTestBase):
    fulfillment_by = FulfillmentBy.ERP

    GIFT = {"name": "Luis Pérez", "company": None, "address1": "Calle Sol 5", "address2": "2ºB", "city": "Sevilla",
            "zip": "41001", "province": "Sevilla", "country": "Spain", "countryCodeV2": "ES", "phone": "+34600111222"}

    def test_the_delivery_order_ships_to_the_orders_shipping_address(self):
        record = self.sync(order(shippingAddress=self.GIFT))
        delivery_order = DeliveryOrder.objects.get()
        address = delivery_order.delivery_address
        self.assertEqual(record.shipping_address, address)
        self.assertEqual(
            (address.street, address.postal_code, address.city, str(address.country), address.contact_name, address.contact_phone),
            ("Calle Sol 5 2ºB", "41001", "Sevilla", "ES", "Luis Pérez", "+34600111222"),
        )
        self.assertEqual(address.client, record.quote.client)
        # The buyer's billing address stays the client's default.
        self.assertEqual(record.quote.client.addresses.get(is_default=True).street, "Calle Mayor 1")

        self.sync(order(id="gid://shopify/Order/2", name="#1002", shippingAddress=self.GIFT))
        self.assertEqual(record.quote.client.addresses.count(), 2)  # the same address is reused

    def test_a_new_address_before_dispatch_moves_the_parcel_and_not_after(self):
        self.sync(order(shippingAddress=self.GIFT))
        moved = {**self.GIFT, "address1": "Avenida Luna 9", "address2": None}
        record = self.sync(order(updatedAt="2026-10-21T10:00:00Z", shippingAddress=moved))
        delivery_order = DeliveryOrder.objects.get()
        self.assertEqual(delivery_order.delivery_address.street, "Avenida Luna 9")

        DeliveryOrderFulfillmentService.dispatch(delivery_order)
        self.sync(order(updatedAt="2026-10-21T11:00:00Z", shippingAddress=self.GIFT))
        delivery_order.refresh_from_db()
        self.assertEqual(delivery_order.delivery_address.street, "Avenida Luna 9")  # already on its way
        self.assertEqual(record.quote.client.pk, delivery_order.client_id)

    def test_orders_without_a_shipping_address_ship_to_the_clients_default(self):
        record = self.sync(order())
        self.assertIsNone(record.shipping_address)
        self.assertEqual(DeliveryOrder.objects.get().delivery_address.street, "Calle Mayor 1")
