"""
A delivery order moves Pending shipment -> Prepared -> Dispatched ->
Delivered, and its goods leave the warehouse when it is dispatched — not when
it is created from the quote.

    python manage.py test sales.tests.test_delivery_order_fulfillment
"""
import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse

from sales.admin.tabs import delivery_order_tabs
from sales.models import (
    ClientAddress,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    DeliveryShippingMethod,
    ShippingAgency,
)
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService as F
from sales.tests.test_delivery_order_components import _ComponentLinesBase


class StockMovesAtDispatchTests(_ComponentLinesBase):

    def setUp(self):
        self.plain = self._item("PLAIN", stock=10)
        self.do = self._deliver(self._accepted_quote_line(self.plain, "4"), dispatch=False)

    def test_creating_the_order_moves_no_stock(self):
        self.assertEqual(self.do.status, DeliveryOrderStatus.DRAFT)
        self.assertEqual(self._stock(self.plain), 10)

    def test_preparing_moves_no_stock(self):
        F.mark_prepared(self.do)
        self.assertEqual(self._stock(self.plain), 10)

    def test_dispatching_takes_the_stock_out(self):
        F.mark_prepared(self.do)
        F.dispatch(self.do)
        self.assertEqual(self._stock(self.plain), 6)
        self.assertEqual(self.do.dispatched_at, datetime.date.today())

    def test_confirming_delivery_does_not_move_stock_again(self):
        F.dispatch(self.do)
        F.confirm_delivery(self.do, received_by="Ana")
        self.assertEqual(self._stock(self.plain), 6)
        self.do.refresh_from_db()
        self.assertEqual(self.do.status, DeliveryOrderStatus.DELIVERED)
        self.assertEqual(self.do.received_by, "Ana")

    def test_undoing_the_dispatch_returns_the_stock(self):
        F.dispatch(self.do)
        F.undo(self.do)
        self.do.refresh_from_db()
        self.assertEqual(self.do.status, DeliveryOrderStatus.PREPARED)
        self.assertIsNone(self.do.dispatched_at)
        self.assertEqual(self._stock(self.plain), 10)

    def test_cancelling_a_dispatched_order_returns_the_stock_once(self):
        F.dispatch(self.do)
        F.cancel(self.do)
        F.reopen(self.do)
        self.assertEqual(self.do.status, DeliveryOrderStatus.DRAFT)
        self.assertEqual(self._stock(self.plain), 10)

    def test_a_line_added_after_dispatch_leaves_at_once(self):
        F.dispatch(self.do)
        extra = self._item("EXTRA", stock=5)
        DeliveryOrderItem.objects.create(delivery_order=self.do, inventory_item=extra, quantity=Decimal("2"))
        self.assertEqual(self._stock(extra), 3)

    def test_an_order_whose_stock_left_under_the_old_rule_is_not_deducted_twice(self):
        # Before dispatch existed, creating the line took the goods out.
        line = self.do.items.get()
        self.do.status = DeliveryOrderStatus.DISPATCHED
        line.delivery_order = self.do
        line._deduct_inventory()
        DeliveryOrder.objects.filter(pk=self.do.pk).update(status=DeliveryOrderStatus.DRAFT)
        self.do.refresh_from_db()
        self.assertEqual(self._stock(self.plain), 6)

        F.dispatch(self.do)
        self.assertEqual(self._stock(self.plain), 6)

        F.cancel(self.do)
        self.assertEqual(self._stock(self.plain), 10)

    def test_transitions_outside_the_lifecycle_are_refused(self):
        with self.assertRaises(ValidationError):
            F.confirm_delivery(self.do)  # never dispatched
        F.cancel(self.do)
        with self.assertRaises(ValidationError):
            F.dispatch(self.do)  # cancelled orders must be reopened first
        with self.assertRaises(ValidationError):
            F.undo(self.do)


class ShippingDefaultsTests(_ComponentLinesBase):

    def test_planned_date_and_address_come_from_the_quote_and_client(self):
        ClientAddress.objects.create(
            client=self.client_obj, company=self.company, address_type="fiscal", is_default=True,
            street="Fiscal 1", city="X", postal_code="1", province="P",
        )
        shipping = ClientAddress.objects.create(
            client=self.client_obj, company=self.company, address_type="shipping",
            street="Obra 7", city="X", postal_code="1", province="P",
        )
        line = self._accepted_quote_line(self._item("P", stock=1), "1")
        line.quote.estimated_delivery_date = datetime.date(2026, 11, 3)
        line.quote.save()

        do = self._deliver(line, dispatch=False)

        self.assertEqual(do.planned_delivery_date, datetime.date(2026, 11, 3))
        self.assertEqual(do.delivery_address, shipping)


class AdminTests(_ComponentLinesBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = User.objects.create_superuser("admin", "a@example.com", "pw")
        cls.agency = ShippingAgency.objects.create(company=cls.company, name="SEUR")

    def setUp(self):
        self.client.force_login(self.superuser)
        self.plain = self._item("PLAIN", stock=10)
        self.do = self._deliver(self._accepted_quote_line(self.plain, "4"), dispatch=False)

    def test_dispatch_form_records_the_shipment_and_moves_stock(self):
        response = self.client.post(reverse("admin:sales_deliveryorder_dispatch", args=[self.do.pk]), {
            "dispatched_at": "2026-09-29",
            "shipping_method": DeliveryShippingMethod.CARRIER,
            "shipping_agency": self.agency.pk,
            "package_count": "3",
            "shipment_number": "SEUR-1",
            "tracking_url": "",
        })
        self.assertRedirects(
            response, reverse("admin:sales_deliveryorder_change", args=[self.do.pk]), fetch_redirect_response=False,
        )
        self.do.refresh_from_db()
        self.assertEqual(self.do.status, DeliveryOrderStatus.DISPATCHED)
        self.assertEqual(self.do.shipping_agency, self.agency)
        self.assertEqual(self.do.package_count, 3)
        self.assertEqual(self._stock(self.plain), 6)

    def test_carrier_dispatch_needs_an_agency(self):
        response = self.client.post(reverse("admin:sales_deliveryorder_dispatch", args=[self.do.pk]), {
            "dispatched_at": "2026-09-29",
            "shipping_method": DeliveryShippingMethod.CARRIER,
        })
        self.assertEqual(response.status_code, 200)
        self.do.refresh_from_db()
        self.assertEqual(self.do.status, DeliveryOrderStatus.DRAFT)

    def test_change_page_offers_only_the_steps_that_apply(self):
        response = self.client.get(reverse("admin:sales_deliveryorder_change", args=[self.do.pk]))
        content = response.content.decode()
        for offered in ("mark_prepared_detail_action", "dispatch_detail_action", "cancel_detail_action"):
            self.assertIn(offered, content)
        for hidden in ("confirm_delivery_detail_action", "undo_status_detail_action", "reopen_detail_action"):
            self.assertNotIn(hidden, content)

    def test_status_badge_keeps_its_colour_when_translated(self):
        from django.contrib.admin.sites import site
        from django.utils import translation
        from unfold.utils import display_for_label

        model_admin = site._registry[DeliveryOrder]
        method = model_admin.get_status_label
        for language in ("es", "en"):
            with self.subTest(language=language), translation.override(language):
                html = display_for_label(method(self.do), "-", method.label)
                self.assertIn(str(DeliveryOrderStatus.DRAFT.label), html)
                self.assertIn("bg-orange-100", html)  # "warning", not the grey fallback

    def test_status_tabs_count_the_pending_queue(self):
        request = RequestFactory().get(reverse("admin:sales_deliveryorder_changelist"), {"status__exact": "draft"})
        request.user = self.superuser
        items = delivery_order_tabs(request)[0]["items"]

        pending, prepared, *_rest, all_tab = items
        self.assertIn(">1<", str(pending["title"]))
        self.assertTrue(pending["active"](request))
        self.assertFalse(all_tab["active"](request))
        self.assertEqual(prepared["title"], DeliveryOrderStatus.PREPARED.label)
