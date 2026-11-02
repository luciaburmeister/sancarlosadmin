from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from django.utils.translation import gettext

from api.fleet.tests.helpers import make_company
from apps.shopify.models import (
    FulfillmentBy,
    InvoiceTiming,
    RefundHandling,
    RefundStatus,
    ShopifyOrder,
    ShopifyStore,
    ShopifyVariantLink,
    VariantLinkStatus,
)
from apps.shopify.services.invoicing import ShopifyInvoicingService
from apps.shopify.services.orders import ShopifyOrderService
from apps.shopify.tests.test_orders import ORDER_ID, money, order
from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction, Warehouse
from invoices.models import InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus, TaxType
from payments.models import Payment, PaymentMethod
from sales.models import DeliveryOrder, DeliveryOrderStatus
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService
from sales.services.external_order import InvoiceKind

GRAPHQL = "apps.shopify.client.ShopifyClient.graphql"
EMAIL = "invoices.services.sales_invoice_service.SalesInvoiceService.queue_client_email"
VARIANT = "gid://shopify/ProductVariant/1"
LINE = "gid://shopify/LineItem/1"
LATER = "2026-10-21T12:00:00Z"


def refund(refund_id="gid://shopify/Refund/1", tanks=1, amount="114.95", shipping=None):
    """A refund of ``tanks`` tanks (95.00 + 21 % each) and, optionally, of the shipping."""
    return {
        "id": refund_id,
        "createdAt": LATER,
        "totalRefundedSet": money(amount),
        "refundLineItems": {"nodes": [
            {"quantity": tanks, "lineItem": {"id": LINE, "name": "Tank 100 L", "variant": {"id": VARIANT},
                                             "taxLines": [{"ratePercentage": 21.0}]}},
        ] if tanks else []},
        "refundShippingLines": {"nodes": [
            {"subtotalAmountSet": money(shipping[0]), "taxAmountSet": money(shipping[1])},
        ] if shipping else []},
    }


def fulfillment(quantity=2):
    return {
        "id": f"gid://shopify/Fulfillment/{quantity}",
        "status": "SUCCESS",
        "trackingInfo": [],
        "fulfillmentLineItems": {"nodes": [{"quantity": quantity, "lineItem": {"variant": {"id": VARIANT}}}]},
    }


class InvoicingTestBase(TestCase):
    fulfillment_by = FulfillmentBy.ERP
    invoice_on = InvoiceTiming.PAYMENT

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Fistex")
        cls.nave = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        cls.tank = InventoryItem.objects.create(company=cls.company, code="T-1", name="Tank", default_warehouse=cls.nave)
        ItemTransaction.objects.create(item=cls.tank, warehouse=cls.nave, movement_type=MovementType.IN, quantity_change=10)
        cls.portes = InventoryItem.objects.create(company=cls.company, code="SRV-1", name="Portes", item_type=ItemType.SERVICE)
        cls.iva = TaxType.objects.create(tax_name="IVA")

    def setUp(self):
        cache.clear()
        self.store = ShopifyStore.objects.create(
            company=self.company, name="Shop", shop_domain="fistex.myshopify.com", access_token_encrypted="x",
            import_orders=True, tax_type=self.iva, shipping_item=self.portes, fulfillment_by=self.fulfillment_by,
            invoice_on=self.invoice_on, scopes="read_orders,read_customers",
        )
        # Orders of the fixture were placed on 2026-10-21: invoicing was on before them.
        ShopifyStore.objects.filter(pk=self.store.pk).update(invoicing_since="2026-10-01T00:00Z")
        self.store.refresh_from_db()
        ShopifyVariantLink.objects.create(
            store=self.store, product_id="p", variant_id=VARIANT, sku="T-1",
            item=self.tank, status=VariantLinkStatus.MATCHED, synced_at="2026-10-21T08:00Z",
        )

    def configure(self, **settings):
        for name, value in settings.items():
            setattr(self.store, name, value)
        self.store.save()

    def sync(self, payload):
        with mock.patch(GRAPHQL, return_value=payload), mock.patch(EMAIL) as email, \
                self.captureOnCommitCallbacks(execute=True):
            record = ShopifyOrderService.sync_order(self.store, ORDER_ID)
        self.emails = email.call_count
        return record

    def invoices(self):
        return SalesInvoice.objects.filter(company=self.company).exclude(status=SalesInvoiceStatus.CANCELLED).order_by("pk")


class InvoiceWhenPaidTests(InvoicingTestBase):
    def test_paid_order_gets_a_pro_forma_for_its_exact_total_billing_its_delivery(self):
        record = self.sync(order())

        invoice = record.invoice
        self.assertEqual((invoice.status, invoice.total_amount.amount), (SalesInvoiceStatus.PROFORMA, Decimal("235.95")))
        self.assertEqual((invoice.subtotal_amount.amount, invoice.tax_amount.amount), (Decimal("195.00"), Decimal("40.95")))
        self.assertEqual(sorted(invoice.items.values_list("reference", flat=True)), ["SRV-1", "T-1"])
        self.assertEqual(DeliveryOrder.objects.get(external_id=ORDER_ID).sales_invoice, invoice)
        # The ERP's normal flow: everything hangs off the order's own expediente.
        project_file = record.quote.project_file
        self.assertEqual((project_file.name, project_file.client), ("Shopify #1001", record.quote.client))
        self.assertEqual((invoice.project_file, DeliveryOrder.objects.get(external_id=ORDER_ID).project_file), (project_file, project_file))
        self.assertEqual((self.emails, Payment.objects.count()), (0, 0))  # a pro forma waits for review

        self.sync(order(updatedAt=LATER))
        self.assertEqual(self.invoices().count(), 1)

    def test_unpaid_orders_and_stores_invoicing_by_hand_get_no_invoice(self):
        self.assertIsNone(self.sync(order(displayFinancialStatus="PENDING")).invoice)

        self.configure(invoice_on=InvoiceTiming.MANUAL)
        self.assertIsNone(self.store.invoicing_since)
        self.assertIsNone(self.sync(order(updatedAt=LATER)).invoice)
        self.assertFalse(self.invoices().exists())

    def test_orders_placed_before_invoicing_was_turned_on_are_left_to_staff(self):
        ShopifyStore.objects.filter(pk=self.store.pk).update(invoicing_since=timezone.now())
        self.store.refresh_from_db()
        self.assertIsNone(self.sync(order()).invoice)

    def test_issued_automatically_emailed_and_paid(self):
        self.configure(issue_invoices=True, email_invoices=True)
        invoice = self.sync(order()).invoice
        invoice.refresh_from_db()

        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertTrue(invoice.invoice_number)
        self.assertEqual(self.emails, 1)
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)
        payment = Payment.objects.get()
        self.assertEqual((payment.method, payment.amount.amount, payment.client), (PaymentMethod.CARD, Decimal("235.95"), invoice.client))

    def test_no_email_and_no_payment_when_the_store_says_so(self):
        self.configure(issue_invoices=True, email_invoices=False, record_payments=False)
        invoice = self.sync(order()).invoice
        self.assertEqual((invoice.status, self.emails, Payment.objects.count()), (SalesInvoiceStatus.FINAL, 0, 0))

    def test_payment_is_recorded_once_staff_issue_the_pro_forma(self):
        record = self.sync(order())
        from invoices.services.sales_invoice_service import SalesInvoiceService

        SalesInvoiceService.finalize_invoice(record.invoice, send_email=False)
        self.assertEqual(ShopifyInvoicingService.invoice_pending(self.store), 1)
        record.invoice.refresh_from_db()
        self.assertEqual(record.invoice.payment_status, InvoicePaymentStatus.PAID)
        self.assertEqual(ShopifyInvoicingService.invoice_pending(self.store), 0)

    def test_a_failure_keeps_the_order_and_is_retried(self):
        with mock.patch("sales.services.external_order.ExternalOrderService.invoice_kind", return_value=InvoiceKind.SIMPLIFIED):
            record = self.sync(order())
        self.assertIsNotNone(record.quote)
        self.assertEqual(record.invoicing_error, gettext("Simplified invoices are not supported yet."))
        self.assertIsNone(ShopifyOrder.objects.get().invoice)

        self.assertEqual(ShopifyInvoicingService.invoice_pending(self.store), 1)
        record.refresh_from_db()
        self.assertEqual((record.invoicing_error, record.invoice.total_amount.amount), ("", Decimal("235.95")))


class RefundTests(InvoicingTestBase):
    def test_refund_after_invoicing_becomes_a_credit_note_and_the_delivery_shrinks(self):
        self.configure(refund_handling=RefundHandling.CREDIT_NOTE)
        record = self.sync(order())
        data = order(updatedAt=LATER, refunds=[refund()], totalRefundedSet=money("114.95"), currentTotalPriceSet=money("121.00"))
        data["order"]["lineItems"]["nodes"][0]["currentQuantity"] = 1
        record = self.sync(data)

        credited = record.refunds.get()
        self.assertEqual(credited.status, RefundStatus.CREDITED)
        credit_note = credited.credit_note
        self.assertEqual((credit_note.subtotal_amount.amount, credit_note.total_amount.amount), (Decimal("-95.00"), Decimal("-114.95")))
        self.assertEqual(list(credit_note.items.values_list("reference", "quantity")), [("T-1", Decimal("-1"))])
        self.assertEqual(record.invoice.total_amount.amount, Decimal("235.95"))  # the invoice stands
        # Not shipped yet: the pending delivery now holds only what is still sold.
        pending = DeliveryOrder.objects.get(external_id=ORDER_ID, status=DeliveryOrderStatus.DRAFT)
        self.assertEqual(pending.items.get(inventory_item=self.tank).quantity, Decimal("1"))
        self.assertEqual(pending.sales_invoice, record.invoice)
        self.assertTrue(record.totals_match)

        self.sync(order(updatedAt="2026-10-21T13:00:00Z", refunds=[refund()], totalRefundedSet=money("114.95"),
                        currentTotalPriceSet=money("121.00"),
                        lineItems=data["order"]["lineItems"]))
        self.assertEqual(SalesInvoice.objects.filter(total_amount__lt=0).count(), 1)  # credited once

    def test_returned_goods_after_shipping_are_credited_and_stay_on_the_quote(self):
        self.configure(refund_handling=RefundHandling.CREDIT_NOTE)
        self.sync(order())
        DeliveryOrderFulfillmentService.dispatch(DeliveryOrder.objects.get(external_id=ORDER_ID))
        data = order(updatedAt=LATER, refunds=[refund(tanks=2, amount="235.95", shipping=("6.05", "1.05"))],
                     totalRefundedSet=money("235.95"), currentTotalPriceSet=money("0.00"))
        data["order"]["lineItems"]["nodes"][0]["currentQuantity"] = 0
        record = self.sync(data)

        self.assertEqual(record.error, "")
        self.assertEqual(record.quote.inventory_items.get(inventory_item=self.tank).quantity, Decimal("2"))
        self.assertTrue(record.totals_match)  # compared before returns
        credit_note = record.refunds.get().credit_note
        self.assertEqual(credit_note.total_amount.amount, Decimal("-235.95"))
        self.assertEqual(sorted(credit_note.items.values_list("reference", flat=True)), ["SRV-1", "T-1"])

    def test_review_by_hand_and_amounts_that_are_not_whole_products(self):
        record = self.sync(order())
        data = order(updatedAt=LATER, refunds=[refund()])
        data["order"]["lineItems"]["nodes"][0]["currentQuantity"] = 1
        record = self.sync(data)
        self.assertEqual(record.refunds.get().status, RefundStatus.REVIEW)

        self.configure(refund_handling=RefundHandling.CREDIT_NOTE)
        self.sync(order(updatedAt="2026-10-21T13:00:00Z", refunds=[refund(), refund("gid://shopify/Refund/2", tanks=0, amount="10.00")],
                        lineItems=data["order"]["lineItems"]))
        goodwill = record.refunds.get(refund_id="gid://shopify/Refund/2")
        self.assertEqual(goodwill.status, RefundStatus.REVIEW)
        self.assertEqual(goodwill.note, gettext("The refund isn't for products or shipping; settle it by hand."))
        self.assertFalse(SalesInvoice.objects.filter(total_amount__lt=0).exists())

    def test_refund_before_invoicing_only_changes_the_quote(self):
        self.configure(refund_handling=RefundHandling.CREDIT_NOTE)
        data = order(displayFinancialStatus="PARTIALLY_REFUNDED", refunds=[refund()], currentTotalPriceSet=money("121.00"))
        data["order"]["lineItems"]["nodes"][0]["currentQuantity"] = 1
        record = self.sync(data)

        self.assertEqual(record.refunds.get().status, RefundStatus.NO_CREDIT)
        self.assertEqual(record.invoice.total_amount.amount, Decimal("121.00"))
        self.assertFalse(SalesInvoice.objects.filter(total_amount__lt=0).exists())


class InvoiceEachShipmentTests(InvoicingTestBase):
    fulfillment_by = FulfillmentBy.SHOPIFY
    invoice_on = InvoiceTiming.SHIPMENT

    def test_each_shipment_made_in_shopify_gets_its_own_invoice(self):
        record = self.sync(order(fulfillments=[fulfillment(1)]))
        self.assertIsNone(record.invoice)
        first = DeliveryOrder.objects.get(external_id=ORDER_ID)
        self.assertEqual(first.sales_invoice.total_amount.amount, Decimal("121.00"))  # 1 tank + shipping

        self.sync(order(updatedAt=LATER, fulfillments=[fulfillment(1), {**fulfillment(1), "id": "gid://shopify/Fulfillment/9"}]))
        totals = sorted(invoice.total_amount.amount for invoice in self.invoices())
        self.assertEqual(totals, [Decimal("114.95"), Decimal("121.00")])


class ErpShipmentInvoicingTests(InvoicingTestBase):
    invoice_on = InvoiceTiming.SHIPMENT

    def test_dispatching_in_the_erp_queues_the_invoice_after_commit(self):
        self.sync(order())
        delivery_order = DeliveryOrder.objects.get(external_id=ORDER_ID)
        self.assertIsNone(delivery_order.sales_invoice)  # pending: nothing shipped yet

        with mock.patch("apps.shopify.tasks.invoice_shipment.delay") as delay, \
                mock.patch("apps.shopify.tasks.push_fulfillment.delay"), \
                self.captureOnCommitCallbacks(execute=True):
            DeliveryOrderFulfillmentService.dispatch(delivery_order)
            delay.assert_not_called()
        delay.assert_called_once_with(delivery_order.pk)

        from apps.shopify.tasks import invoice_shipment

        invoice_shipment(delivery_order.pk)
        delivery_order.refresh_from_db()
        self.assertEqual(delivery_order.sales_invoice.total_amount.amount, Decimal("235.95"))

    def test_the_periodic_check_invoices_shipments_it_missed(self):
        self.sync(order())
        with mock.patch("apps.shopify.tasks.invoice_shipment.delay"), mock.patch("apps.shopify.tasks.push_fulfillment.delay"):
            DeliveryOrderFulfillmentService.dispatch(DeliveryOrder.objects.get(external_id=ORDER_ID))
        self.assertEqual(ShopifyInvoicingService.invoice_pending(self.store), 1)
        self.assertEqual(self.invoices().count(), 1)
        self.assertEqual(ShopifyInvoicingService.invoice_pending(self.store), 0)


class InvoicingSettingsTests(TestCase):
    def test_invoicing_starts_when_turned_on_and_restarts_after_turning_off(self):
        store = ShopifyStore.objects.create(company=make_company(name="Fistex"), name="Shop", shop_domain="f.myshopify.com")
        self.assertIsNone(store.invoicing_since)
        store.invoice_on = InvoiceTiming.PAYMENT
        store.save(update_fields=["invoice_on"])
        store.refresh_from_db()
        started = store.invoicing_since
        self.assertIsNotNone(started)

        store.invoice_on = InvoiceTiming.SHIPMENT
        store.save()
        self.assertEqual(store.invoicing_since, started)

        store.invoice_on = InvoiceTiming.MANUAL
        store.save()
        self.assertIsNone(store.invoicing_since)
