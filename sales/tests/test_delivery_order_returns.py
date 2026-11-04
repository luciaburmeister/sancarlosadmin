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
from inventory.models import ItemIdentityCode
from sales.models import (
    DeliveryOrderItem,
    DeliveryOrderReturn,
    DeliveryOrderReturnCode,
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


class IdentityCodeTests(_ReturnsBase):
    """Codes that come back are unlinked from the delivery order when the
    return is received, and linked back if it is cancelled."""

    def setUp(self):
        super().setUp()
        self.codes = [
            ItemIdentityCode.objects.create(item=self.plain, code=f"SN{i}", delivery_order_item=self.line)
            for i in range(1, 5)
        ]

    def _pick(self, ret, *codes):
        line = ret.items.get()
        for code in codes:
            DeliveryOrderReturnCode.objects.create(
                return_item=line, identity_code=code, delivery_order_item=code.delivery_order_item,
            )

    def _linked(self):
        return set(
            ItemIdentityCode.objects.filter(delivery_order_item=self.line).values_list("code", flat=True)
        )

    def test_a_draft_keeps_its_codes_linked(self):
        self._pick(self._return((self.line, 2, self.excess)), *self.codes[:2])
        self.assertEqual(self._linked(), {"SN1", "SN2", "SN3", "SN4"})

    def test_receiving_unlinks_the_returned_codes_only(self):
        ret = self._return((self.line, 2, self.excess))
        self._pick(ret, *self.codes[:2])
        R.receive(ret)
        self.assertEqual(self._linked(), {"SN3", "SN4"})
        returned = ItemIdentityCode.objects.get(code="SN1")
        self.assertIsNone(returned.delivered_at)
        self.assertEqual(returned.return_links.get().delivery_order_item, self.line)

    def test_a_restocking_reason_leaves_the_code_free_to_ship(self):
        ret = self._return((self.line, 1, self.excess))
        self._pick(ret, self.codes[0])
        R.receive(ret)
        self.assertFalse(ItemIdentityCode.objects.get(code="SN1").is_blocked)

    def test_a_reason_that_does_not_restock_blocks_the_code(self):
        ret = self._return((self.line, 1, self.damaged))
        self._pick(ret, self.codes[0])
        R.receive(ret)
        code = ItemIdentityCode.objects.get(code="SN1")
        self.assertNotIn("SN1", self._linked())
        self.assertTrue(code.is_blocked)
        self.assertTrue(code.return_links.get().blocked)

    def test_cancelling_lifts_the_block_it_set(self):
        ret = self._return((self.line, 1, self.damaged))
        self._pick(ret, self.codes[0])
        R.receive(ret)
        R.cancel(ret)
        code = ItemIdentityCode.objects.get(code="SN1")
        self.assertEqual(code.delivery_order_item, self.line)
        self.assertFalse(code.is_blocked)

    def test_cancelling_keeps_a_block_set_by_hand(self):
        ItemIdentityCode.objects.filter(code="SN1").update(is_blocked=True)
        ret = self._return((self.line, 1, self.damaged))
        self._pick(ret, ItemIdentityCode.objects.get(code="SN1"))
        R.receive(ret)
        R.cancel(ret)
        self.assertTrue(ItemIdentityCode.objects.get(code="SN1").is_blocked)

    def test_cancelling_links_them_back(self):
        ret = self._return((self.line, 2, self.excess))
        self._pick(ret, *self.codes[:2])
        R.receive(ret)
        R.cancel(ret)
        self.assertEqual(self._linked(), {"SN1", "SN2", "SN3", "SN4"})
        self.assertIsNotNone(ItemIdentityCode.objects.get(code="SN1").delivered_at)

    def test_cancel_is_refused_once_a_code_shipped_again(self):
        ret = self._return((self.line, 1, self.excess))
        self._pick(ret, self.codes[0])
        R.receive(ret)
        other = self._deliver(self._accepted_quote_line(self.plain, "1"))
        ItemIdentityCode.objects.filter(code="SN1").update(delivery_order_item=other.items.get())

        with self.assertRaises(ValidationError):
            R.cancel(ret)
        ret.refresh_from_db()
        self.assertEqual(ret.status, DeliveryOrderReturnStatus.RECEIVED)
        self.assertEqual(self._stock(self.plain), 6)  # 6 - 1 reshipped + 1 still returned

    def test_receive_is_refused_when_a_code_was_unlinked_meanwhile(self):
        ret = self._return((self.line, 1, self.excess))
        self._pick(ret, self.codes[0])
        ItemIdentityCode.objects.filter(code="SN1").update(delivery_order_item=None)
        with self.assertRaises(ValidationError):
            R.receive(ret)
        self.assertEqual(self._stock(self.plain), 6)

    def test_never_more_codes_than_units(self):
        ret = self._return((self.line, 1, self.excess))
        self._pick(ret, *self.codes[:2])
        with self.assertRaises(ValidationError):
            R.receive(ret)
        self.assertEqual(self._linked(), {"SN1", "SN2", "SN3", "SN4"})

    def test_a_code_of_another_line_is_refused(self):
        extra = self._item("EXTRA", stock=5)
        extra_line = DeliveryOrderItem.objects.create(delivery_order=self.do, inventory_item=extra, quantity=2)
        foreign = ItemIdentityCode.objects.create(item=extra, code="X1", delivery_order_item=extra_line)
        errors = R.check_codes([(self.line, 1, [foreign])])
        self.assertEqual(len(errors), 1)

    def test_a_kit_brings_back_its_components_codes_in_proportion(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))
        top = self._parent_line(do)
        handle_line = self._component(top, handle)  # 3 handles shipped
        codes = [
            ItemIdentityCode.objects.create(item=handle, code=f"H{i}", delivery_order_item=handle_line)
            for i in range(3)
        ]
        self.assertEqual(R.check_codes([(top, 2, codes[:2])]), [])
        self.assertEqual(len(R.check_codes([(top, 2, codes)])), 1)  # 2 kits → 2 handles

        ret = self._return((top, 2, self.excess), do=do)
        line = ret.items.get()
        for code in codes[:2]:
            DeliveryOrderReturnCode.objects.create(
                return_item=line, identity_code=code, delivery_order_item=handle_line,
            )
        R.receive(ret)
        self.assertEqual(handle_line.identity_codes.count(), 1)


class IdentityCodeAdminTests(_ReturnsBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = User.objects.create_superuser("admin", "a@example.com", "pw")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)
        self.codes = [
            ItemIdentityCode.objects.create(item=self.plain, code=f"SN{i}", delivery_order_item=self.line)
            for i in range(1, 4)
        ]

    def _post(self, quantity, codes):
        url = reverse("admin:sales_deliveryorderreturn_add") + f"?delivery_order={self.do.pk}"
        return self.client.post(url, {
            "delivery_order": self.do.pk, "date": "2026-11-04", "notes": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-delivery_order_item": self.line.pk,
            "items-0-quantity": quantity,
            "items-0-reason": self.excess.pk,
            "items-0-codes": [code.pk for code in codes],
        })

    def test_picked_codes_are_stored_and_unlinked_on_receive(self):
        response = self._post(2, self.codes[:2])
        self.assertEqual(response.status_code, 302)
        ret = DeliveryOrderReturn.objects.get()
        link_codes = set(ret.items.get().returned_codes.values_list("identity_code__code", flat=True))
        self.assertEqual(link_codes, {"SN1", "SN2"})

        self.client.get(reverse("admin:sales_deliveryorderreturn_receive_detail_action", args=[ret.pk]))
        self.assertEqual(self.line.identity_codes.count(), 1)

        page = self.client.get(reverse("admin:sales_deliveryorderreturn_change", args=[ret.pk]))
        self.assertContains(page, "SN1")
        do_page = self.client.get(reverse("admin:sales_deliveryorder_change", args=[self.do.pk]))
        self.assertContains(do_page, "SN2")

    def test_more_codes_than_units_is_refused_on_the_draft(self):
        response = self._post(1, self.codes[:2])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DeliveryOrderReturn.objects.exists())

    def test_a_returned_code_can_be_shipped_again(self):
        self._post(1, self.codes[:1])
        ret = DeliveryOrderReturn.objects.get()
        self.client.get(reverse("admin:sales_deliveryorderreturn_receive_detail_action", args=[ret.pk]))

        next_do = self._deliver(self._accepted_quote_line(self.plain, "1"))
        next_line = next_do.items.get()
        link_url = reverse("admin:sales_deliveryorder_link_identity_codes", args=[next_do.pk])
        page = self.client.get(link_url, {"target_item": next_line.pk})
        self.assertIn(self.codes[0], page.context["candidates"])

        self.client.post(link_url, {"target_item": next_line.pk, "codes": [self.codes[0].pk]})
        self.codes[0].refresh_from_db()
        self.assertEqual(self.codes[0].delivery_order_item, next_line)

    def test_a_blocked_code_is_not_offered_or_linked_again(self):
        url = reverse("admin:sales_deliveryorderreturn_add") + f"?delivery_order={self.do.pk}"
        self.client.post(url, {
            "delivery_order": self.do.pk, "date": "2026-11-04", "notes": "",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-delivery_order_item": self.line.pk,
            "items-0-quantity": 1,
            "items-0-reason": self.damaged.pk,
            "items-0-codes": [self.codes[0].pk],
        })
        ret = DeliveryOrderReturn.objects.get()
        self.client.get(reverse("admin:sales_deliveryorderreturn_receive_detail_action", args=[ret.pk]))

        next_do = self._deliver(self._accepted_quote_line(self.plain, "1"))
        next_line = next_do.items.get()
        link_url = reverse("admin:sales_deliveryorder_link_identity_codes", args=[next_do.pk])
        page = self.client.get(link_url, {"target_item": next_line.pk})
        self.assertNotIn(self.codes[0], page.context["candidates"])

        self.client.post(link_url, {"target_item": next_line.pk, "codes": [self.codes[0].pk]})
        self.codes[0].refresh_from_db()
        self.assertIsNone(self.codes[0].delivery_order_item)
        self.assertTrue(self.codes[0].is_blocked)
