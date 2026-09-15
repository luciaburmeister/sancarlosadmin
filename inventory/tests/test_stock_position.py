"""
inventory/tests/test_stock_position.py

InventoryItem.incoming_quantity / reserved_quantity — the "on its way" and
"promised" figures shown next to stock on hand — and the queryset annotations
that compute the same numbers in bulk for the item list.

Run:
    python manage.py test inventory.tests.test_stock_position
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase

from company.models import Company
from inventory.choices import ItemType, MovementType
from inventory.models import BillOfMaterialsLine, InventoryItem, ItemTransaction, Warehouse
from inventory.services.stock_position import (
    find_drift,
    refresh_all,
    refresh_stock_position,
)
from invoices.services.delivery_order_service import DeliveryOrderService
from management.models import ProjectFile, ProjectFileStatus
from materials.models import Provider
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderReceipt,
    PurchaseOrderReceiptItem,
    PurchaseOrderStatus,
    PurchaseOrderType,
)
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderStatus,
    Quote,
    QuoteInventoryItem,
    QuoteStatus,
)


class _StockPositionTestCase(TestCase):
    """Shared fixtures: one company, one warehouse, one supplier, one client."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.warehouse = Warehouse.objects.create(
            code="WH1", name="Main", company=cls.company,
        )
        cls.provider = Provider.objects.create(company=cls.company, name="Proveedor SA")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        cls.project_file = ProjectFile.objects.create(
            name="PF", company=cls.company, client=cls.client_obj,
        )
        cls.quote_line_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

    def _make_item(self, code, stock=0):
        item = InventoryItem.objects.create(
            code=code,
            name=code,
            company=self.company,
            item_type=ItemType.STOCKABLE,
            default_warehouse=self.warehouse,
        )
        if stock:
            ItemTransaction.objects.create(
                item=item,
                warehouse=self.warehouse,
                movement_type=MovementType.IN,
                quantity_change=stock,
                note="Initial stock",
            )
        return item

    # ── Purchasing side ──────────────────────────────────────────────────

    def _order(self, item, quantity, status=PurchaseOrderStatus.APPROVED):
        po = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            po_type=PurchaseOrderType.STOCK,
            status=status,
        )
        return PurchaseOrderItem.objects.create(
            purchase_order=po,
            inventory_item=item,
            quantity=Decimal(quantity),
            unit_price=Decimal("10"),
        )

    def _receive(self, po_item, quantity):
        receipt = PurchaseOrderReceipt.objects.create(
            company=self.company,
            purchase_order=po_item.purchase_order,
            warehouse=self.warehouse,
        )
        return PurchaseOrderReceiptItem.objects.create(
            receipt=receipt,
            po_item=po_item,
            quantity_received=Decimal(quantity),
        )

    # ── Sales side ───────────────────────────────────────────────────────

    def _accepted_quote_line(self, item, quantity, status=QuoteStatus.ACCEPTED,
                             project_file=None, no_project_file=False):
        quote = Quote.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=None if no_project_file else (project_file or self.project_file),
            project_name="Q",
            status=status,
        )
        return QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=item,
            quantity=Decimal(quantity),
            unit_price=Decimal("100"),
        )

    def _deliver(self, quote_line, quantity=None):
        return DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=[{
                "source_content_type_id": self.quote_line_ct.id,
                "source_object_id": quote_line.pk,
                "description": quote_line.inventory_item.name,
                "quantity": quantity if quantity is not None else quote_line.quantity,
                "unit_price": quote_line.unit_price.amount,
            }],
        )

    # ── Assertions ───────────────────────────────────────────────────────

    def assertPosition(self, item, *, on_hand, incoming, reserved):
        """Assert the figures, and that the cache agrees with a live recompute.

        Every test checks both the stored columns (what the app reads) and a
        fresh recomputation from source (what they should hold). That is what
        makes each of these a sync test as well as a rule test: if a signal
        failed to fire, the stored value and the computed one diverge here.
        """
        fresh = (
            InventoryItem.objects
            .with_stock_on_hand()
            .with_computed_stock_position()
            .get(pk=item.pk)
        )

        self.assertEqual(fresh.stock_on_hand, on_hand)
        self.assertEqual(fresh.incoming_quantity, Decimal(incoming))
        self.assertEqual(fresh.reserved_quantity, Decimal(reserved))

        # The cache is in sync with source.
        self.assertEqual(fresh.incoming_quantity, fresh.computed_incoming)
        self.assertEqual(fresh.reserved_quantity, fresh.computed_reserved)
        self.assertFalse(find_drift().exists())


# ─────────────────────────────────────────────────────────────────────────────
# Incoming — ordered but not yet received
# ─────────────────────────────────────────────────────────────────────────────

class IncomingQuantityTest(_StockPositionTestCase):

    def test_no_orders_means_nothing_incoming(self):
        item = self._make_item("INC-0", stock=5)
        self.assertPosition(item, on_hand=5, incoming=0, reserved=0)

    def test_approved_order_counts_as_incoming(self):
        item = self._make_item("INC-1", stock=5)
        self._order(item, "10")
        self.assertPosition(item, on_hand=5, incoming="10", reserved=0)

    def test_receiving_moves_quantity_from_incoming_to_on_hand(self):
        item = self._make_item("INC-2", stock=5)
        po_item = self._order(item, "10")
        self._receive(po_item, "4")
        self.assertPosition(item, on_hand=9, incoming="6", reserved=0)

    def test_fully_received_order_leaves_nothing_incoming(self):
        item = self._make_item("INC-3")
        po_item = self._order(item, "10")
        self._receive(po_item, "10")
        self.assertPosition(item, on_hand=10, incoming=0, reserved=0)

    def test_draft_and_pending_orders_are_not_incoming(self):
        item = self._make_item("INC-4")
        self._order(item, "7", status=PurchaseOrderStatus.DRAFT)
        self._order(item, "3", status=PurchaseOrderStatus.PENDING_APPROVAL)
        self.assertPosition(item, on_hand=0, incoming=0, reserved=0)

    def test_cancelled_and_rejected_orders_are_not_incoming(self):
        item = self._make_item("INC-5")
        self._order(item, "7", status=PurchaseOrderStatus.CANCELLED)
        self._order(item, "3", status=PurchaseOrderStatus.REJECTED)
        self.assertPosition(item, on_hand=0, incoming=0, reserved=0)

    def test_incoming_sums_across_orders_and_lines(self):
        item = self._make_item("INC-6")
        self._order(item, "10")
        po_item = self._order(item, "5")
        self._receive(po_item, "2")
        self.assertPosition(item, on_hand=2, incoming="13", reserved=0)

    def test_incoming_keeps_fractional_quantities(self):
        item = self._make_item("INC-7")
        self._order(item, "2.500")
        self.assertPosition(item, on_hand=0, incoming="2.500", reserved=0)

    def test_orders_for_other_items_do_not_leak(self):
        item = self._make_item("INC-8")
        other = self._make_item("INC-8-OTHER")
        self._order(other, "99")
        self.assertPosition(item, on_hand=0, incoming=0, reserved=0)


# ─────────────────────────────────────────────────────────────────────────────
# Reserved — accepted but not yet delivered
# ─────────────────────────────────────────────────────────────────────────────

class ReservedQuantityTest(_StockPositionTestCase):

    def test_accepted_quote_reserves_stock_without_moving_it(self):
        item = self._make_item("RES-1", stock=20)
        self._accepted_quote_line(item, "6")
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

    def test_draft_and_sent_quotes_reserve_nothing(self):
        item = self._make_item("RES-2", stock=20)
        self._accepted_quote_line(item, "6", status=QuoteStatus.DRAFT)
        self._accepted_quote_line(item, "4", status=QuoteStatus.SENT)
        self.assertPosition(item, on_hand=20, incoming=0, reserved=0)

    def test_rejected_quote_reserves_nothing(self):
        item = self._make_item("RES-3", stock=20)
        self._accepted_quote_line(item, "6", status=QuoteStatus.REJECTED)
        self.assertPosition(item, on_hand=20, incoming=0, reserved=0)

    def test_partial_delivery_releases_that_part_of_the_reservation(self):
        item = self._make_item("RES-4", stock=20)
        line = self._accepted_quote_line(item, "6")
        self._deliver(line, Decimal("2"))
        # Two units shipped: stock drops and the reservation drops with it.
        self.assertPosition(item, on_hand=18, incoming=0, reserved="4")

    def test_full_delivery_clears_the_reservation(self):
        item = self._make_item("RES-5", stock=20)
        line = self._accepted_quote_line(item, "6")
        self._deliver(line)
        self.assertPosition(item, on_hand=14, incoming=0, reserved=0)

    def test_cancelling_the_delivery_restores_the_reservation(self):
        item = self._make_item("RES-6", stock=20)
        line = self._accepted_quote_line(item, "6")
        delivery_order = self._deliver(line)

        delivery_order.status = DeliveryOrderStatus.CANCELLED
        delivery_order.save()

        # Cancelling returns the goods, so they are promised again.
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

    def test_reserved_sums_across_quotes(self):
        item = self._make_item("RES-7", stock=20)
        self._accepted_quote_line(item, "6")
        self._accepted_quote_line(item, "2.500")
        self.assertPosition(item, on_hand=20, incoming=0, reserved="8.500")

    def test_reservation_can_exceed_stock_on_hand(self):
        item = self._make_item("RES-8", stock=2)
        self._accepted_quote_line(item, "10")
        self.assertPosition(item, on_hand=2, incoming=0, reserved="10")
        self.assertEqual(
            InventoryItem.objects.get(pk=item.pk).available_quantity,
            Decimal("-8"),
        )

    def test_quotes_for_other_items_do_not_leak(self):
        item = self._make_item("RES-9", stock=5)
        other = self._make_item("RES-9-OTHER", stock=5)
        self._accepted_quote_line(other, "3")
        self.assertPosition(item, on_hand=5, incoming=0, reserved=0)

    # ── Project-file lifecycle is the release valve ──────────────────────

    def test_completing_the_project_releases_the_reservation(self):
        item = self._make_item("PF-1", stock=20)
        self._accepted_quote_line(item, "6")
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

        self.project_file.status = ProjectFileStatus.COMPLETED
        self.project_file.save()
        self.assertPosition(item, on_hand=20, incoming=0, reserved=0)

    def test_cancelling_the_project_releases_the_reservation(self):
        item = self._make_item("PF-2", stock=20)
        self._accepted_quote_line(item, "6")

        self.project_file.status = ProjectFileStatus.CANCELLED
        self.project_file.save()
        self.assertPosition(item, on_hand=20, incoming=0, reserved=0)

    def test_reopening_the_project_restores_the_reservation(self):
        item = self._make_item("PF-3", stock=20)
        self._accepted_quote_line(item, "6")

        self.project_file.status = ProjectFileStatus.COMPLETED
        self.project_file.save()
        self.project_file.status = ProjectFileStatus.ACTIVE
        self.project_file.save()
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

    def test_on_hold_project_still_reserves(self):
        item = self._make_item("PF-4", stock=20)
        self._accepted_quote_line(item, "6")

        self.project_file.status = ProjectFileStatus.ON_HOLD
        self.project_file.save()
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

    def test_quote_without_project_file_stays_reserved(self):
        """The exclude() over a nullable FK must keep NULL project files."""
        item = self._make_item("PF-5", stock=20)
        self._accepted_quote_line(item, "6", no_project_file=True)
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

    def test_moving_a_quote_to_a_closed_project_releases_it(self):
        item = self._make_item("PF-6", stock=20)
        line = self._accepted_quote_line(item, "6")
        closed = ProjectFile.objects.create(
            name="Closed", company=self.company, client=self.client_obj,
            status=ProjectFileStatus.COMPLETED,
        )

        line.quote.project_file = closed
        line.quote.save()
        self.assertPosition(item, on_hand=20, incoming=0, reserved=0)

    def test_over_delivery_does_not_drive_reserved_negative(self):
        item = self._make_item("RES-10", stock=20)
        line = self._accepted_quote_line(item, "4")
        # A delivery order line built by hand can ship more than was quoted.
        DeliveryOrder.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
        ).items.create(
            description=item.name,
            quantity=Decimal("9"),
            unit_price=Decimal("100"),
            source_content_type=self.quote_line_ct,
            source_object_id=line.pk,
        )
        self.assertPosition(item, on_hand=11, incoming=0, reserved=0)


# ─────────────────────────────────────────────────────────────────────────────
# The two figures together
# ─────────────────────────────────────────────────────────────────────────────

class ReservedThroughBillOfMaterialsTest(_StockPositionTestCase):
    """A quoted kit reserves its components: BOM quantity × the kit's own
    reservation, on top of anything the component is quoted for directly.
    Production case: a "Termosifón" kit of three parts on an accepted quote
    showed its KSC 300 component with stock on hand and nothing reserved.
    """

    def _bom(self, parent, component, quantity):
        return BillOfMaterialsLine.objects.create(
            parent=parent, component=component, quantity=quantity,
        )

    def _kit(self, component_qty=2, stock=0):
        kit = self._make_item("KIT")
        part = self._make_item("PART", stock=stock)
        self._bom(kit, part, component_qty)
        return kit, part

    def test_quoting_the_kit_reserves_its_components(self):
        kit, part = self._kit(component_qty=1, stock=2)
        self._accepted_quote_line(kit, 1)
        self.assertPosition(kit, on_hand=0, incoming=0, reserved=1)
        self.assertPosition(part, on_hand=2, incoming=0, reserved=1)
        self.assertEqual(InventoryItem.objects.get(pk=part.pk).available_quantity, Decimal("1"))

    def test_component_reservation_scales_with_bom_and_quoted_quantity(self):
        kit, part = self._kit(component_qty=3)
        self._accepted_quote_line(kit, 2)
        self.assertPosition(part, on_hand=0, incoming=0, reserved=6)

    def test_draft_quote_reserves_no_components(self):
        kit, part = self._kit()
        self._accepted_quote_line(kit, 1, status=QuoteStatus.DRAFT)
        self.assertPosition(part, on_hand=0, incoming=0, reserved=0)

    def test_direct_and_kit_reservations_add_up(self):
        kit, part = self._kit(component_qty=2)
        self._accepted_quote_line(kit, 1)
        self._accepted_quote_line(part, 5)
        self.assertPosition(part, on_hand=0, incoming=0, reserved=7)

    def test_nested_kits_reserve_all_the_way_down(self):
        kit = self._make_item("KIT")
        sub = self._make_item("SUB")
        part = self._make_item("PART")
        self._bom(kit, sub, 2)
        self._bom(sub, part, 3)
        self._accepted_quote_line(kit, 2)
        self.assertPosition(kit, on_hand=0, incoming=0, reserved=2)
        self.assertPosition(sub, on_hand=0, incoming=0, reserved=4)
        self.assertPosition(part, on_hand=0, incoming=0, reserved=12)

    def test_delivering_the_kit_ships_and_releases_the_components(self):
        kit, part = self._kit(component_qty=2, stock=5)
        line = self._accepted_quote_line(kit, 1)
        self.assertPosition(part, on_hand=5, incoming=0, reserved=2)
        do = self._deliver(line)
        # The BOM cascade took the parts out; the promise is fulfilled.
        self.assertPosition(part, on_hand=3, incoming=0, reserved=0)
        do.status = DeliveryOrderStatus.CANCELLED
        do.save()
        self.assertPosition(part, on_hand=5, incoming=0, reserved=2)

    def test_partial_delivery_releases_that_share_of_the_components(self):
        kit, part = self._kit(component_qty=2, stock=10)
        line = self._accepted_quote_line(kit, 3)
        self._deliver(line, quantity=1)
        self.assertPosition(kit, on_hand=-1, incoming=0, reserved=2)
        self.assertPosition(part, on_hand=8, incoming=0, reserved=4)

    def test_completing_the_project_releases_the_components(self):
        kit, part = self._kit(component_qty=2)
        self._accepted_quote_line(kit, 1)
        self.project_file.status = ProjectFileStatus.COMPLETED
        self.project_file.save()
        self.assertPosition(part, on_hand=0, incoming=0, reserved=0)

    def test_changing_the_bom_quantity_refreshes_the_component(self):
        kit, part = self._kit(component_qty=2)
        self._accepted_quote_line(kit, 1)
        bom = kit.bom_lines.get()
        bom.quantity = 5
        bom.save()
        self.assertPosition(part, on_hand=0, incoming=0, reserved=5)

    def test_removing_the_component_from_the_bom_releases_it(self):
        kit, part = self._kit(component_qty=2)
        self._accepted_quote_line(kit, 1)
        kit.bom_lines.get().delete()
        self.assertPosition(part, on_hand=0, incoming=0, reserved=0)

    def test_swapping_the_bom_component_moves_the_reservation(self):
        kit, part = self._kit(component_qty=2)
        other = self._make_item("OTHER")
        self._accepted_quote_line(kit, 1)
        bom = kit.bom_lines.get()
        bom.component = other
        bom.save()
        self.assertPosition(part, on_hand=0, incoming=0, reserved=0)
        self.assertPosition(other, on_hand=0, incoming=0, reserved=2)

    def test_adding_a_component_to_an_already_quoted_kit_reserves_it(self):
        kit = self._make_item("KIT")
        self._accepted_quote_line(kit, 2)
        late = self._make_item("LATE")
        self._bom(kit, late, 1)
        self.assertPosition(late, on_hand=0, incoming=0, reserved=2)

    def test_kits_for_other_components_do_not_leak(self):
        kit, part = self._kit(component_qty=2)
        bystander = self._make_item("BYSTANDER")
        self._accepted_quote_line(kit, 1)
        self.assertPosition(bystander, on_hand=0, incoming=0, reserved=0)


class StockPositionCombinedTest(_StockPositionTestCase):

    def test_incoming_and_reserved_are_independent(self):
        item = self._make_item("BOTH-1", stock=10)
        self._order(item, "25")
        self._accepted_quote_line(item, "4")
        self.assertPosition(item, on_hand=10, incoming="25", reserved="4")

    def test_aggregates_do_not_multiply_each_other(self):
        """Three aggregates over three different relations, each with several
        rows — a join-based implementation would inflate all three."""
        item = self._make_item("BOTH-2", stock=10)
        self._order(item, "5")
        self._order(item, "5")
        self._accepted_quote_line(item, "3")
        self._accepted_quote_line(item, "3")
        self.assertPosition(item, on_hand=10, incoming="10", reserved="6")

    def test_item_with_no_activity_reports_zeroes(self):
        item = self._make_item("EMPTY")
        self.assertPosition(item, on_hand=0, incoming=0, reserved=0)


# ─────────────────────────────────────────────────────────────────────────────
# The cache: reads are free, and stay in sync
# ─────────────────────────────────────────────────────────────────────────────

class StockPositionCacheTest(_StockPositionTestCase):

    def test_reading_a_page_of_items_costs_one_query(self):
        """The point of caching them: incoming and reserved are plain columns,
        so a list of items needs no per-row work for them."""
        first = self._make_item("BULK-1", stock=4)
        second = self._make_item("BULK-2", stock=7)
        self._order(first, "3")
        self._accepted_quote_line(second, "2")

        with self.assertNumQueries(1):
            position = {
                i.code: (i.stock_on_hand, i.incoming_quantity, i.reserved_quantity)
                for i in InventoryItem.objects.with_stock_on_hand()
                .filter(pk__in=[first.pk, second.pk])
            }

        self.assertEqual(position["BULK-1"], (4, Decimal("3"), Decimal("0")))
        self.assertEqual(position["BULK-2"], (7, Decimal("0"), Decimal("2")))

    def test_refresh_repairs_a_row_corrupted_behind_the_cache(self):
        """Deliberate drift — the recovery path the whole design rests on."""
        item = self._make_item("HEAL-1", stock=10)
        self._order(item, "8")
        self._accepted_quote_line(item, "3")

        InventoryItem.objects.filter(pk=item.pk).update(
            incoming_quantity=Decimal("999"), reserved_quantity=Decimal("999"),
        )
        drifted = find_drift()
        self.assertEqual([i.pk for i in drifted], [item.pk])

        refresh_stock_position([item.pk])
        item.refresh_from_db()

        self.assertEqual(item.incoming_quantity, Decimal("8"))
        self.assertEqual(item.reserved_quantity, Decimal("3"))
        self.assertFalse(find_drift().exists())

    def test_refresh_all_repairs_every_item(self):
        first = self._make_item("HEAL-2", stock=5)
        second = self._make_item("HEAL-3", stock=5)
        self._order(first, "4")
        self._accepted_quote_line(second, "2")

        InventoryItem.objects.update(
            incoming_quantity=Decimal("0"), reserved_quantity=Decimal("0"),
        )
        self.assertEqual(find_drift().count(), 2)

        refresh_all()

        self.assertFalse(find_drift().exists())
        self.assertEqual(
            InventoryItem.objects.get(pk=first.pk).incoming_quantity, Decimal("4"),
        )
        self.assertEqual(
            InventoryItem.objects.get(pk=second.pk).reserved_quantity, Decimal("2"),
        )

    def test_refresh_stamps_the_time(self):
        item = self._make_item("STAMP")
        self.assertIsNone(item.stock_position_refreshed_at)
        refresh_stock_position([item.pk])
        item.refresh_from_db()
        self.assertIsNotNone(item.stock_position_refreshed_at)

    def test_full_save_of_a_stale_instance_does_not_clobber_the_cache(self):
        """An admin edit or an import loads the row, then full-saves it. If a
        refresh landed in between, the instance's copy of the cached columns
        is stale and must not be written back."""
        item = self._make_item("CLOBBER", stock=5)
        stale = InventoryItem.objects.get(pk=item.pk)  # incoming == 0 here

        self._order(item, "12")  # refresh sets incoming == 12 in the DB

        stale.name = "Renamed"
        stale.save()

        fresh = InventoryItem.objects.get(pk=item.pk)
        self.assertEqual(fresh.name, "Renamed")
        self.assertEqual(fresh.incoming_quantity, Decimal("12"))
        self.assertFalse(find_drift().exists())

    # ── Re-pointing a line must refresh the item it left ─────────────────

    def test_switching_a_quote_line_to_another_item_releases_the_first(self):
        first = self._make_item("SWAP-Q1", stock=20)
        second = self._make_item("SWAP-Q2", stock=20)
        line = self._accepted_quote_line(first, "6")
        self.assertPosition(first, on_hand=20, incoming=0, reserved="6")

        line.inventory_item = second
        line.save()

        self.assertPosition(first, on_hand=20, incoming=0, reserved=0)
        self.assertPosition(second, on_hand=20, incoming=0, reserved="6")

    def test_switching_a_po_line_to_another_item_releases_the_first(self):
        first = self._make_item("SWAP-P1")
        second = self._make_item("SWAP-P2")
        po_item = self._order(first, "9")
        self.assertPosition(first, on_hand=0, incoming="9", reserved=0)

        po_item.inventory_item = second
        po_item.save()

        self.assertPosition(first, on_hand=0, incoming=0, reserved=0)
        self.assertPosition(second, on_hand=0, incoming="9", reserved=0)

    def test_changing_a_quote_line_quantity_refreshes(self):
        item = self._make_item("QTY-1", stock=20)
        line = self._accepted_quote_line(item, "6")

        line.quantity = Decimal("2")
        line.save()
        self.assertPosition(item, on_hand=20, incoming=0, reserved="2")

    def test_saves_that_cannot_change_the_figures_do_not_refresh(self):
        """The discount engine re-saves every line wholesale, and each line
        save re-saves its totals. Neither can change quantity or item, so
        neither may cost a refresh — the refresh timestamp is the witness."""
        item = self._make_item("NARROW", stock=20)
        line = self._accepted_quote_line(item, "6")
        stamped = InventoryItem.objects.get(pk=item.pk).stock_position_refreshed_at
        self.assertIsNotNone(stamped)

        line.save(update_fields=["discount"])          # narrow, unrelated field
        line.discount = Decimal("5")
        line.save()                                    # full save, nothing watched changed

        self.assertEqual(
            InventoryItem.objects.get(pk=item.pk).stock_position_refreshed_at, stamped,
        )
        self.assertPosition(item, on_hand=20, incoming=0, reserved="6")

    def test_rollback_takes_the_cache_write_with_it(self):
        """The refresh runs inside the caller's transaction, so an aborted
        purchase order cannot leave the cache claiming stock is on its way."""
        item = self._make_item("ROLLBACK", stock=5)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with transaction.atomic():
                self._order(item, "12")
                self.assertEqual(
                    InventoryItem.objects.get(pk=item.pk).incoming_quantity,
                    Decimal("12"),
                )
                raise Boom

        self.assertEqual(
            InventoryItem.objects.get(pk=item.pk).incoming_quantity, Decimal("0"),
        )
        self.assertFalse(find_drift().exists())
