"""
Invoicing delivery orders (flow E):

- an order whose invoice was cancelled is invoiceable again (listing,
  builder and admin action), can be synced, is listed as not invoiced, and
  the new invoice takes over its link;
- the invoice deducts the expediente's advances (default plan): it keeps
  the order's lines, discount and taxes, its total goes down by the
  deduction, and the order itself is left as it is;
- an order with only one VAT group's goods closes that group's part of the
  advance;
- re-invoicing an order after its invoice was cancelled deducts again;
- the admin action's message names the deduction;
- in client-tax mode the advance and the order's invoice tax every source
  with the quote's header taxes, as the quote and the order do.
"""
from datetime import date
from decimal import Decimal

from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from configurator.models import Configuration
from invoices.models import AppliedTax, SalesInvoice, SalesInvoiceStatus
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from sales.admin.delivery_order import DeliveryOrderAdmin
from sales.models import DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus


class CancelledInvoiceDeliveryOrderTests(AdvanceExpedienteMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.delivery_order = DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=DeliveryOrderStatus.DISPATCHED,
        )
        DeliveryOrderItem.objects.create(
            delivery_order=self.delivery_order, description=self.config.name,
            quantity=Decimal("1"), unit_price=Decimal("1000.00"),
            source_content_type=ContentType.objects.get_for_model(Configuration),
            source_object_id=self.config.pk,
        )

    def _invoice(self):
        return DeliveryOrderService.create_invoice_from_delivery_orders(
            [self.delivery_order], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )

    def _invoiceable(self):
        return list(DeliveryOrderService.get_invoiceable_delivery_orders(self.project_file))

    def _cancel(self, invoice):
        for status in (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED):
            invoice.status = status
            invoice.save(update_fields=["status"])

    def test_invoiced_order_is_not_invoiced_twice(self):
        self.assertEqual(self._invoiceable(), [self.delivery_order])
        self._invoice()
        self.assertEqual(self._invoiceable(), [])
        self.delivery_order.refresh_from_db()
        with self.assertRaises(ValidationError):
            self._invoice()

    def test_cancelled_invoice_frees_the_order(self):
        first = self._invoice()
        self._cancel(first)
        self.delivery_order.refresh_from_db()
        self.assertEqual(self._invoiceable(), [self.delivery_order])

        second = self._invoice()

        self.delivery_order.refresh_from_db()
        self.assertEqual(self.delivery_order.sales_invoice, second)
        self.assertEqual(self._invoiceable(), [])
        self.assertEqual(self.summary()["goods_remaining_net"], Decimal("0.00"))

    def test_a_cancelled_invoice_does_not_block_syncing(self):
        self._cancel(self._invoice())
        self.delivery_order.refresh_from_db()

        DeliveryOrderService.sync_from_sources(self.delivery_order)

        self.assertEqual(self._invoiceable(), [self.delivery_order])

    def test_admin_action_invoices_it_again(self):
        self.client.force_login(User.objects.create_superuser("do-admin", "d@x.com", "pw"))
        url = reverse(
            "admin:sales_deliveryorder_create_invoice_from_do_row_action",
            args=[self.delivery_order.pk],
        )
        first = self._invoice()

        response = self.client.get(url)
        self.assertEqual(
            [m.level for m in get_messages(response.wsgi_request)], [messages.WARNING]
        )
        self.assertEqual(SalesInvoice.objects.filter(project_file=self.project_file).count(), 1)

        self._cancel(first)
        response = self.client.get(url)
        self.delivery_order.refresh_from_db()
        second = self.delivery_order.sales_invoice
        self.assertNotEqual(second, first)
        self.assertRedirects(
            response, reverse("admin:invoices_salesinvoice_change", args=[second.pk]),
            fetch_redirect_response=False,
        )

    def test_admin_list_shows_it_as_not_invoiced(self):
        model_admin = DeliveryOrderAdmin(DeliveryOrder, admin.site)

        def invoiced():
            order = DeliveryOrder.objects.select_related("sales_invoice").get(pk=self.delivery_order.pk)
            return model_admin.get_invoiced_label(order)[0]

        self.assertFalse(invoiced())
        first = self._invoice()
        self.assertTrue(invoiced())
        self._cancel(first)
        # As in the invoiceable filter: a cancelled invoice bills nothing
        self.assertFalse(invoiced())

        self.client.force_login(User.objects.create_superuser("do-admin", "d@x.com", "pw"))
        self.assertEqual(self.client.get(reverse("admin:sales_deliveryorder_changelist")).status_code, 200)


class DeliveryOrderDeductionTests(AdvanceExpedienteMixin, TestCase):
    def _order(self, config, amount):
        order = DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=DeliveryOrderStatus.DISPATCHED,
        )
        DeliveryOrderItem.objects.create(
            delivery_order=order, description=config.name,
            quantity=Decimal("1"), unit_price=amount,
            source_content_type=ContentType.objects.get_for_model(Configuration),
            source_object_id=config.pk,
        )
        return order

    def _invoice(self, order):
        return DeliveryOrderService.create_invoice_from_delivery_orders(
            [order], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )

    @staticmethod
    def _snapshot(order):
        order = DeliveryOrder.objects.get(pk=order.pk)
        return (
            order.subtotal_amount, order.discount_amount, order.tax_amount, order.total_amount,
            order.status, order.discount_breakdown,
            [(i.description, i.quantity, i.unit_price, i.line_total) for i in order.items.order_by("pk")],
            [
                (t.tax_type_id, t.selected_percentage, t.base_amount, t.calculated_amount)
                for t in order.applied_taxes.order_by("pk")
            ],
        )

    @staticmethod
    def _taxes(invoice):
        return {
            tax.selected_percentage: tax.base_amount.amount
            for tax in AppliedTax.objects.filter(
                content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
            )
        }

    def test_invoice_deducts_the_advance_and_leaves_the_order_alone(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        order = self._order(self.config, Decimal("1000.00"))
        before = self._snapshot(order)

        invoice = self._invoice(order)

        [goods] = invoice.items.filter(advance_line__isnull=True)
        self.assertEqual((goods.description, goods.line_total.amount), (self.config.name, Decimal("1000.00")))
        [deduction] = AdvanceDeductionService.deduction_lines(invoice)
        self.assertEqual((deduction.advance_line, deduction.line_total.amount), (line, Decimal("-300.00")))
        self.assertIsNone(deduction.project_file)
        self.assertEqual(self._taxes(invoice), {Decimal("21.00"): Decimal("700.00")})
        self.assertEqual(
            (invoice.subtotal_amount.amount, invoice.tax_amount.amount, invoice.total_amount.amount),
            (Decimal("700.00"), Decimal("147.00"), Decimal("847.00")),
        )
        self.assertEqual(self._snapshot(order), before)
        order.refresh_from_db()
        self.assertEqual(order.sales_invoice, invoice)
        self.assertEqual(self.summary()["advances_open_net"], Decimal("0.00"))

    def test_an_order_of_one_vat_group_closes_that_part_of_the_advance(self):
        config_10 = self.add_config(self.quote, Decimal("100.00"), percentage=Decimal("10"))
        advance = AdvanceInvoiceService.create(
            self.project_file, date_issued=date(2026, 9, 1), percentage=Decimal("30"),
        )
        self.set_status(advance, SalesInvoiceStatus.FINAL)
        line_10 = advance.items.get(unit_price=Decimal("30.00"))

        invoice = self._invoice(self._order(config_10, Decimal("100.00")))

        [deduction] = AdvanceDeductionService.deduction_lines(invoice)
        self.assertEqual((deduction.advance_line, deduction.line_total.amount), (line_10, Decimal("-30.00")))
        self.assertEqual(self._taxes(invoice), {Decimal("10.00"): Decimal("70.00")})
        # The IVA 21 % part waits for the goods of its group
        self.assertEqual(self.summary()["advances_open_net"], Decimal("300.00"))

    def test_re_invoicing_after_a_cancelled_invoice_deducts_again(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        order = self._order(self.config, Decimal("1000.00"))
        first = self._invoice(order)
        for status in (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED):
            first.status = status
            first.save(update_fields=["status"])
        order.refresh_from_db()

        second = self._invoice(order)

        [deduction] = AdvanceDeductionService.deduction_lines(second)
        self.assertEqual((deduction.advance_line, deduction.line_total.amount), (line, Decimal("-300.00")))
        self.assertEqual(second.total_amount.amount, Decimal("847.00"))
        self.assertEqual(self.summary()["advances_open_net"], Decimal("0.00"))

    def test_admin_message_names_the_deduction(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        order = self._order(self.config, Decimal("1000.00"))
        self.client.force_login(User.objects.create_superuser("do-admin", "d@x.com", "pw"))

        response = self.client.get(
            reverse("admin:sales_deliveryorder_create_invoice_from_do_row_action", args=[order.pk])
        )

        [message] = get_messages(response.wsgi_request)
        self.assertEqual(message.level, messages.SUCCESS)
        self.assertIn(advance.display_number, message.message)
        order.refresh_from_db()
        self.assertEqual(len(AdvanceDeductionService.deduction_lines(order.sales_invoice)), 1)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class ClientTaxModeTests(AdvanceExpedienteMixin, TestCase):
    """A company taxing its quotes in client-tax mode: the quote's header
    IVA 21 % taxes its whole subtotal (1,200.00 → 1,452.00), a 200.00
    configuration with no tax rows of its own included. Its advance and its
    delivery order's invoice tax it the same way."""

    def setUp(self):
        super().setUp()
        self.company.auto_apply_client_taxes = True
        self.company.save(update_fields=["auto_apply_client_taxes"])
        self.untaxed = self.add_config(self.quote, Decimal("200.00"))
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(Configuration), object_id=self.untaxed.pk,
        ).delete()
        self.quote.recalc_totals()
        self.quote.refresh_from_db()

    def test_advance_and_delivery_order_invoice_add_up_to_the_quote(self):
        self.assertEqual(self.quote.total_price.amount, Decimal("1452.00"))
        advance = SalesInvoiceService.finalize_invoice(
            AdvanceInvoiceService.create(
                self.project_file, date_issued=date(2026, 9, 1), percentage=Decimal("30"),
            ),
            send_email=False,
        )
        # All of it at IVA 21 %, as the quote taxes it
        self.assertEqual(advance.items.count(), 1)
        self.assertEqual(advance.total_amount.amount, Decimal("435.60"))

        order = DeliveryOrderService.create_delivery_order(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            items_data=[
                {**item, "quantity": item["remaining_quantity"]}
                for item in DeliveryOrderService.get_deliverable_items(self.project_file)
            ],
        )
        self.assertEqual(order.total_amount.amount, Decimal("1452.00"))
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [order], company=self.company, client=self.client_obj, date_issued=date(2026, 9, 29),
        )
        invoice = SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(invoice.total_amount.amount, Decimal("1016.40"))
        self.assertEqual(
            advance.total_amount.amount + invoice.total_amount.amount, self.quote.total_price.amount,
        )
