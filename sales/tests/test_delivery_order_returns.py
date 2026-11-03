"""
Goods a client sends back from a dispatched delivery order. Receiving the
return puts the returned part back in stock — unless its reason is set not
to restock — and cancelling it takes the goods out again.

    python manage.py test sales.tests.test_delivery_order_returns
"""
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.urls import reverse

from inventory.choices import ItemType
from sales.models import (
    DeliveryOrderItem,
    DeliveryOrderReturn,
    DeliveryOrderReturnItem,
    DeliveryOrderReturnStatus,
    DeliveryOrderStatus,
    ReturnReason,
)
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService as F
from sales.services.delivery_order_return_service import DeliveryOrderReturnService as R
from sales.tests.test_delivery_order_components import _ComponentLinesBase


class _ReturnsBase(_ComponentLinesBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.excess = ReturnReason.objects.create(company=cls.company, name="Excess material")
        cls.damaged = ReturnReason.objects.create(company=cls.company, name="Damaged", restocks=False)

    def setUp(self):
        self.plain = self._item("PLAIN", stock=10)
        self.do = self._deliver(self._accepted_quote_line(self.plain, "4"))  # dispatched: stock 6
        self.line = self.do.items.get()

    def _return(self, *lines, do=None):
        ret = DeliveryOrderReturn.objects.create(delivery_order=do or self.do)
        for line, quantity, reason in lines:
            DeliveryOrderReturnItem.objects.create(
                delivery_order_return=ret, delivery_order_item=line, quantity=quantity, reason=reason,
            )
        return ret


class ReceivingTests(_ReturnsBase):

    def test_a_draft_return_moves_no_stock(self):
        self._return((self.line, 3, self.excess))
        self.assertEqual(self._stock(self.plain), 6)

    def test_receiving_puts_the_returned_part_back(self):
        ret = self._return((self.line, 3, self.excess))
        R.receive(ret)
        ret.refresh_from_db()
        self.assertEqual(ret.status, DeliveryOrderReturnStatus.RECEIVED)
        self.assertIsNotNone(ret.received_at)
        self.assertEqual(self._stock(self.plain), 9)
        self.assertTrue(ret.items.get().stock_returned)
        self.assertEqual(self.plain.transactions.filter(reference_object_id=ret.pk).count(), 1)

    def test_a_reason_that_does_not_restock_records_the_return_only(self):
        ret = self._return((self.line, 2, self.damaged))
        R.receive(ret)
        self.assertEqual(self._stock(self.plain), 6)
        self.assertFalse(ret.items.get().stock_returned)

    def test_mixed_reasons_restock_only_their_part(self):
        R.receive(self._return((self.line, 1, self.excess), (self.line, 2, self.damaged)))
        self.assertEqual(self._stock(self.plain), 7)

    def test_never_more_than_shipped_across_returns(self):
        R.receive(self._return((self.line, 3, self.damaged)))  # counts even without restocking
        with self.assertRaises(ValidationError):
            R.receive(self._return((self.line, 2, self.excess)))
        self.assertEqual(self._stock(self.plain), 6)

    def test_a_cancelled_return_frees_its_quantity(self):
        first = self._return((self.line, 4, self.excess))
        R.receive(first)
        R.cancel(first)
        R.receive(self._return((self.line, 4, self.excess)))
        self.assertEqual(self._stock(self.plain), 10)

    def test_an_empty_return_cannot_be_received(self):
        with self.assertRaises(ValidationError):
            R.receive(self._return())

    def test_goods_of_an_order_not_dispatched_cannot_come_back(self):
        do = self._deliver(self._accepted_quote_line(self.plain, "2"), dispatch=False)
        ret = self._return((do.items.get(), 1, self.excess), do=do)
        with self.assertRaises(ValidationError):
            R.receive(ret)

    def test_a_line_that_moved_no_stock_cannot_come_back(self):
        service = self._item("SRV", item_type=ItemType.SERVICE)
        service_line = DeliveryOrderItem.objects.create(
            delivery_order=self.do, inventory_item=service, quantity=1,
        )
        with self.assertRaises(ValidationError):
            R.receive(self._return((service_line, 1, self.excess)))

    def test_a_kit_returns_its_components(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))
        self.assertEqual((self._stock(handle), self._stock(profile)), (47, 976))

        R.receive(self._return((self._parent_line(do), 2, self.excess), do=do))
        self.assertEqual((self._stock(handle), self._stock(profile)), (49, 992))
        self.assertEqual(self._stock(window), 10)  # the kit itself holds no goods

    def test_numbers_run_per_company(self):
        first, second = self._return(), self._return()
        self.assertTrue(first.number.startswith("RET-"))
        self.assertEqual(second.incremental_id, first.incremental_id + 1)
        self.assertEqual(first.company, self.company)


class CancellingTests(_ReturnsBase):

    def test_cancelling_a_received_return_takes_the_goods_out_again(self):
        ret = self._return((self.line, 3, self.excess))
        R.receive(ret)
        R.cancel(ret)
        self.assertEqual(self._stock(self.plain), 6)
        self.assertFalse(ret.items.get().stock_returned)

    def test_cancelling_a_non_restocking_return_moves_nothing(self):
        ret = self._return((self.line, 3, self.damaged))
        R.receive(ret)
        R.cancel(ret)
        self.assertEqual(self._stock(self.plain), 6)

    def test_a_reason_changed_after_receiving_does_not_unbalance_the_cancel(self):
        ret = self._return((self.line, 3, self.excess))
        R.receive(ret)
        ReturnReason.objects.filter(pk=self.excess.pk).update(restocks=False)
        R.cancel(DeliveryOrderReturn.objects.get(pk=ret.pk))
        self.assertEqual(self._stock(self.plain), 6)

    def test_reopened_return_can_be_received_again(self):
        ret = self._return((self.line, 2, self.excess))
        R.cancel(ret)
        R.reopen(ret)
        R.receive(ret)
        self.assertEqual(self._stock(self.plain), 8)

    def test_a_received_return_cannot_be_received_twice(self):
        ret = self._return((self.line, 2, self.excess))
        R.receive(ret)
        with self.assertRaises(ValidationError):
            R.receive(ret)
        self.assertEqual(self._stock(self.plain), 8)


class DeliveryOrderGuardTests(_ReturnsBase):

    def test_undoing_the_dispatch_is_blocked_by_a_received_return(self):
        R.receive(self._return((self.line, 3, self.excess)))
        with self.assertRaises(ValidationError):
            F.undo(self.do)
        with self.assertRaises(ValidationError):
            F.cancel(self.do)
        self.assertEqual(self._stock(self.plain), 9)

    def test_after_cancelling_the_return_the_order_can_go_back(self):
        ret = self._return((self.line, 3, self.excess))
        R.receive(ret)
        R.cancel(ret)
        F.cancel(self.do)
        self.assertEqual(self._stock(self.plain), 10)

    def test_confirming_delivery_is_not_blocked(self):
        R.receive(self._return((self.line, 1, self.excess)))
        F.confirm_delivery(self.do)
        self.assertEqual(self.do.status, DeliveryOrderStatus.DELIVERED)


class AdminTests(_ReturnsBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = User.objects.create_superuser("admin", "a@example.com", "pw")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def _formset(self, *rows):
        data = {
            "items-TOTAL_FORMS": str(len(rows)), "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
        }
        for i, (line, quantity, reason) in enumerate(rows):
            data.update({
                f"items-{i}-delivery_order_item": line.pk,
                f"items-{i}-quantity": quantity,
                f"items-{i}-reason": reason.pk,
            })
        return data

    def test_delivery_order_offers_register_return_once_dispatched(self):
        response = self.client.get(reverse("admin:sales_deliveryorder_change", args=[self.do.pk]))
        self.assertContains(response, "register_return_detail_action")

    def test_add_page_creates_a_draft_with_its_lines(self):
        url = reverse("admin:sales_deliveryorderreturn_add") + f"?delivery_order={self.do.pk}"
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {
            "delivery_order": self.do.pk, "date": "2026-11-03", "notes": "",
            **self._formset((self.line, 2, self.excess)),
        })
        ret = DeliveryOrderReturn.objects.get()
        self.assertRedirects(
            response, reverse("admin:sales_deliveryorderreturn_change", args=[ret.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(ret.items.get().quantity, 2)
        self.assertEqual(self._stock(self.plain), 6)

    def test_add_page_refuses_more_than_was_shipped(self):
        url = reverse("admin:sales_deliveryorderreturn_add") + f"?delivery_order={self.do.pk}"
        response = self.client.post(url, {
            "delivery_order": self.do.pk, "date": "2026-11-03", "notes": "",
            **self._formset((self.line, 5, self.excess)),
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeliveryOrderReturn.objects.exists())

    def test_receive_button_restocks(self):
        ret = self._return((self.line, 2, self.excess))
        change = reverse("admin:sales_deliveryorderreturn_change", args=[ret.pk])
        self.assertContains(self.client.get(change), "receive_detail_action")
        self.client.get(reverse("admin:sales_deliveryorderreturn_receive_detail_action", args=[ret.pk]))
        self.assertEqual(self._stock(self.plain), 8)
        self.assertEqual(self.client.get(change).status_code, 200)
