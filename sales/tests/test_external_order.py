from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from inventory.models import InventoryItem, Warehouse
from invoices.models import AppliedTax, TaxType
from sales.models import Client, Quote, QuoteInventoryItem, QuoteStatus
from sales.services.external_order import (
    ExternalCustomer,
    ExternalOrder,
    ExternalOrderError,
    ExternalOrderLine,
    ExternalOrderService,
)


class ExternalOrderServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Fistex")
        cls.other_company = Company.objects.create(name="Other")
        warehouse = Warehouse.objects.create(company=cls.company, code="NAVE", name="Nave")
        # The item's own price must not leak into a channel order.
        cls.tank = InventoryItem.objects.create(
            company=cls.company, code="T-1", name="Tank", default_warehouse=warehouse, selling_price=Decimal("999")
        )
        cls.probe = InventoryItem.objects.create(company=cls.company, code="P-1", name="Probe", default_warehouse=warehouse)
        cls.iva = TaxType.objects.create(tax_name="IVA")
        cls.client_ = Client.objects.create(company=cls.company, name="Web customer", email="ana@example.com")

    def order(self, paid=True, lines=None):
        return ExternalOrder(
            company=self.company,
            client=self.client_,
            reference="gid://shopify/Order/1",
            name="Shopify #1001",
            date=date(2026, 10, 21),
            paid=paid,
            tax_type=self.iva,
            lines=lines or [ExternalOrderLine(item=self.tank, quantity=Decimal("2"), net_total=Decimal("190.00"), tax_rate=Decimal("21"))],
        )

    def test_quote_keeps_the_channels_exact_amounts_and_tax(self):
        quote = ExternalOrderService.sync_quote(self.order())

        self.assertTrue(quote.prices_from_channel)
        self.assertEqual((quote.status, quote.external_id), (QuoteStatus.ACCEPTED, "gid://shopify/Order/1"))
        line = quote.inventory_items.get()
        self.assertEqual(line.line_total.amount, Decimal("190.00"))
        self.assertEqual([(t.tax_type, t.selected_percentage) for t in line.get_taxes()], [(self.iva, Decimal("21.00"))])
        self.assertEqual((quote.subtotal_price.amount, quote.tax_amount.amount, quote.total_price.amount),
                         (Decimal("190.00"), Decimal("39.90"), Decimal("229.90")))

    def test_discount_rules_do_not_touch_channel_prices(self):
        basket = mock.Mock(run=mock.Mock(return_value=(Decimal("19.00"), [{"rule": "10%"}])))
        with mock.patch("sales.services.discount_levels.DiscountLevelRegistry.post_subtotal_handlers", return_value=[basket]):
            quote = ExternalOrderService.sync_quote(self.order())
            basket.run.assert_not_called()
            self.assertEqual(quote.discount_amount.amount, Decimal("0"))

            # An ordinary quote still gets its discounts.
            ordinary = Quote.objects.create(company=self.company, client=self.client_)
            ordinary.recalc_totals()
            basket.run.assert_called_once()

    def test_unpaid_order_waits_and_is_accepted_once_paid(self):
        quote = ExternalOrderService.sync_quote(self.order(paid=False))
        self.assertEqual(quote.status, QuoteStatus.SENT)
        quote = ExternalOrderService.sync_quote(self.order(paid=True), quote)
        self.assertEqual(quote.status, QuoteStatus.ACCEPTED)
        self.assertIsNotNone(quote.date_accepted)

    def test_changed_order_replaces_lines_without_leaving_their_taxes(self):
        quote = ExternalOrderService.sync_quote(self.order())
        old_line = quote.inventory_items.get()
        new_lines = [ExternalOrderLine(item=self.probe, quantity=Decimal("1"), net_total=Decimal("10.00"), tax_rate=Decimal("21"))]

        quote = ExternalOrderService.sync_quote(self.order(lines=new_lines), quote)

        self.assertEqual(list(quote.inventory_items.values_list("inventory_item", flat=True)), [self.probe.pk])
        line_ct = ContentType.objects.get_for_model(QuoteInventoryItem)
        self.assertFalse(AppliedTax.objects.filter(content_type=line_ct, object_id=old_line.pk).exists())

    def test_order_on_a_delivery_order_is_left_for_staff(self):
        quote = ExternalOrderService.sync_quote(self.order())
        new_lines = [ExternalOrderLine(item=self.probe, quantity=Decimal("1"), net_total=Decimal("10.00"), tax_rate=Decimal("21"))]
        with mock.patch.object(ExternalOrderService, "_has_delivery_orders", return_value=True):
            with self.assertRaises(ExternalOrderError):
                ExternalOrderService.sync_quote(self.order(lines=new_lines), quote)
            with self.assertRaises(ExternalOrderError):
                ExternalOrderService.cancel(quote)
        self.assertEqual(quote.inventory_items.get().inventory_item, self.tank)

    def test_pending_delivery_order_can_be_replaced_but_not_a_dispatched_one(self):
        from invoices.services.delivery_order_service import DeliveryOrderService
        from sales.models import DeliveryOrderStatus
        from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService

        quote = ExternalOrderService.sync_quote(self.order())
        pending = DeliveryOrderService.deliver_remaining(quote.project_file)
        new_lines = [ExternalOrderLine(item=self.probe, quantity=Decimal("1"), net_total=Decimal("10.00"), tax_rate=Decimal("21"))]

        with self.assertRaises(ExternalOrderError):  # only when the caller allows it
            ExternalOrderService.sync_quote(self.order(lines=new_lines), quote)
        quote = ExternalOrderService.sync_quote(self.order(lines=new_lines), quote, replace_pending_deliveries=True)
        pending.refresh_from_db()
        self.assertEqual(pending.status, DeliveryOrderStatus.CANCELLED)

        DeliveryOrderFulfillmentService.dispatch(DeliveryOrderService.deliver_remaining(quote.project_file))
        with self.assertRaises(ExternalOrderError):
            ExternalOrderService.cancel(quote, cancel_pending_deliveries=True)

    def test_a_paid_order_is_accepted_into_its_own_expediente(self):
        quote = ExternalOrderService.sync_quote(self.order(paid=False))
        self.assertIsNone(quote.project_file)  # as in the ERP: an expediente once accepted

        quote = ExternalOrderService.sync_quote(self.order(), quote)
        project_file = quote.project_file
        self.assertEqual((project_file.name, project_file.client, project_file.company), ("Shopify #1001", self.client_, self.company))
        self.assertTrue(project_file.reference)
        self.assertEqual(ExternalOrderService.sync_quote(self.order(), quote).project_file, project_file)  # never a second one

    def test_a_cancelled_order_closes_its_expediente_unless_work_was_done_on_it(self):
        from invoices.services.delivery_order_service import DeliveryOrderService
        from management.models import ProjectFileStatus
        from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService

        quote = ExternalOrderService.sync_quote(self.order())
        ExternalOrderService.cancel(quote)
        self.assertEqual(quote.project_file.status, ProjectFileStatus.CANCELLED)

        shipped = ExternalOrderService.sync_quote(self.order())
        DeliveryOrderFulfillmentService.dispatch(DeliveryOrderService.deliver_remaining(shipped.project_file))
        with self.assertRaises(ExternalOrderError):
            ExternalOrderService.cancel(shipped)
        shipped.project_file.refresh_from_db()
        self.assertNotEqual(shipped.project_file.status, ProjectFileStatus.CANCELLED)

    def test_the_whole_order_is_invoiced_from_its_expediente_with_the_channels_amounts(self):
        quote = ExternalOrderService.sync_quote(self.order())
        invoice = ExternalOrderService.invoice(quote)
        self.assertEqual(invoice.project_file, quote.project_file)
        self.assertEqual((invoice.subtotal_amount.amount, invoice.tax_amount.amount, invoice.total_amount.amount),
                         (Decimal("190.00"), Decimal("39.90"), Decimal("229.90")))
        with self.assertRaises(ExternalOrderError):  # nothing left: never billed twice
            ExternalOrderService.invoice(quote)

    def test_cancel_rejects_the_quote(self):
        quote = ExternalOrderService.sync_quote(self.order())
        ExternalOrderService.cancel(quote)
        quote.refresh_from_db()
        self.assertEqual(quote.status, QuoteStatus.REJECTED)
        with self.assertRaises(ExternalOrderError):
            ExternalOrderService.sync_quote(self.order(), quote)

    def test_clients_are_matched_within_the_company_only(self):
        Client.objects.create(company=self.other_company, name="Elsewhere", email="new@example.com")
        customer = ExternalCustomer(name="New Buyer", email="NEW@example.com", street="Calle 1", city="Madrid", postal_code="28001", province="Madrid")

        client = ExternalOrderService.find_or_create_client(self.company, customer)
        self.assertEqual((client.company, client.name), (self.company, "New Buyer"))
        self.assertEqual(client.addresses.get().city, "Madrid")

        # Same email again: the same client, case-insensitively.
        self.assertEqual(ExternalOrderService.find_or_create_client(self.company, customer), client)
        self.assertEqual(
            ExternalOrderService.find_or_create_client(self.company, ExternalCustomer(name="x", email="ana@example.com")),
            self.client_,
        )
