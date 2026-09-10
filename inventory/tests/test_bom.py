"""
Tests for the Bill of Materials (BOM) feature.

Covers:
  • BillOfMaterialsLine model constraints & validation
  • InventoryItem.deduct_bom_components() — flat, nested, circular
  • AdjustStockView cascade on negative adjustments
  • DeliveryOrderItem._deduct_inventory() cascade on delivery
  • Restock on delivery order cancellation / deletion
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase, RequestFactory
from django.urls import reverse

from company.models import Company
from inventory.choices import ItemType, MovementType
from inventory.models import (
    BillOfMaterialsLine,
    InventoryItem,
    ItemTransaction,
    Warehouse,
)
from invoices.services.delivery_order_service import DeliveryOrderService
from management.models import ProjectFile
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteInventoryItem,
    QuoteStatus,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class _BOMBaseTestCase(TestCase):
    """Shared fixtures for all BOM tests."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.warehouse = Warehouse.objects.create(
            code="WH1",
            name="Main Warehouse",
            company=cls.company,
        )
        cls.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )

    def _make_item(self, code, name="Item", stock=0, **kwargs):
        """Create an InventoryItem and optionally seed it with stock."""
        item = InventoryItem.objects.create(
            code=code,
            name=name,
            company=self.company,
            item_type=kwargs.pop("item_type", ItemType.STOCKABLE),
            default_warehouse=kwargs.pop("default_warehouse", self.warehouse),
            **kwargs,
        )
        if stock:
            ItemTransaction.objects.create(
                item=item,
                warehouse=item.default_warehouse,
                movement_type=MovementType.IN,
                quantity_change=stock,
                note="Initial stock",
            )
        return item

    def _stock(self, item):
        """Return the current stock_on_hand for an item (refreshed)."""
        item.refresh_from_db()
        return item.stock_on_hand


# ─────────────────────────────────────────────────────────────────────────────
# 1. Model & Validation
# ─────────────────────────────────────────────────────────────────────────────

class BillOfMaterialsLineModelTest(_BOMBaseTestCase):
    """Test BOM line creation, constraints, and validation."""

    def test_create_bom_line(self):
        parent = self._make_item("P1", "Assembly")
        comp = self._make_item("C1", "Screw")
        line = BillOfMaterialsLine.objects.create(
            parent=parent, component=comp, quantity=4,
        )
        self.assertEqual(line.quantity, 4)
        self.assertEqual(str(line), "P1 → C1 ×4")

    def test_unique_constraint_prevents_duplicate_component(self):
        parent = self._make_item("P2", "Assembly")
        comp = self._make_item("C2", "Bolt")
        BillOfMaterialsLine.objects.create(
            parent=parent, component=comp, quantity=2,
        )
        with self.assertRaises(IntegrityError):
            BillOfMaterialsLine.objects.create(
                parent=parent, component=comp, quantity=5,
            )

    def test_self_reference_rejected(self):
        item = self._make_item("SELF", "Self-ref item")
        line = BillOfMaterialsLine(
            parent=item, component=item, quantity=1,
        )
        with self.assertRaises(ValidationError) as ctx:
            line.full_clean()
        self.assertIn("component", ctx.exception.message_dict)

    def test_different_components_allowed(self):
        parent = self._make_item("P3", "Assembly")
        c1 = self._make_item("C3A", "Part A")
        c2 = self._make_item("C3B", "Part B")
        BillOfMaterialsLine.objects.create(parent=parent, component=c1, quantity=1)
        BillOfMaterialsLine.objects.create(parent=parent, component=c2, quantity=3)
        self.assertEqual(parent.bom_lines.count(), 2)

    def test_reverse_relation_used_in_bom(self):
        parent = self._make_item("P4")
        comp = self._make_item("C4")
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)
        self.assertEqual(comp.used_in_bom.count(), 1)
        self.assertEqual(comp.used_in_bom.first().parent, parent)


# ─────────────────────────────────────────────────────────────────────────────
# 2. deduct_bom_components()
# ─────────────────────────────────────────────────────────────────────────────

class DeductBOMComponentsTest(_BOMBaseTestCase):
    """Test the InventoryItem.deduct_bom_components() method."""

    def test_flat_bom_deduction(self):
        """One parent with two components — stock is deducted correctly."""
        parent = self._make_item("P-FLAT", stock=10)
        comp_a = self._make_item("C-A", stock=100)
        comp_b = self._make_item("C-B", stock=50)

        BillOfMaterialsLine.objects.create(parent=parent, component=comp_a, quantity=3)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_b, quantity=2)

        parent.deduct_bom_components(
            quantity=5,
            warehouse=self.warehouse,
            user=self.user,
            note="Test flat",
        )

        # comp_a: 100 - (3×5) = 85
        self.assertEqual(self._stock(comp_a), 85)
        # comp_b: 50 - (2×5) = 40
        self.assertEqual(self._stock(comp_b), 40)

    def test_nested_bom_deduction(self):
        """Parent → Sub-assembly → Raw part: all levels are deducted."""
        raw_part = self._make_item("RAW", stock=200)
        sub_assy = self._make_item("SUB", stock=50)
        parent = self._make_item("TOP", stock=10)

        # sub_assy requires 4 raw parts
        BillOfMaterialsLine.objects.create(parent=sub_assy, component=raw_part, quantity=4)
        # parent requires 2 sub-assemblies
        BillOfMaterialsLine.objects.create(parent=parent, component=sub_assy, quantity=2)

        parent.deduct_bom_components(
            quantity=3,
            warehouse=self.warehouse,
        )

        # sub_assy: 50 - (2×3) = 44
        self.assertEqual(self._stock(sub_assy), 44)
        # raw_part: 200 - (4 × (2×3)) = 200 - 24 = 176
        self.assertEqual(self._stock(raw_part), 176)

    def test_no_bom_does_nothing(self):
        """An item with no BOM lines should create no transactions."""
        item = self._make_item("NO-BOM", stock=10)
        tx_count_before = ItemTransaction.objects.count()

        item.deduct_bom_components(quantity=5, warehouse=self.warehouse)

        self.assertEqual(ItemTransaction.objects.count(), tx_count_before)
        self.assertEqual(self._stock(item), 10)  # unchanged

    def test_circular_reference_guard(self):
        """Circular BOMs (A→B→A) must not infinite loop."""
        a = self._make_item("CIRC-A", stock=100)
        b = self._make_item("CIRC-B", stock=100)

        BillOfMaterialsLine.objects.create(parent=a, component=b, quantity=1)
        BillOfMaterialsLine.objects.create(parent=b, component=a, quantity=1)

        # Should terminate without error
        a.deduct_bom_components(quantity=2, warehouse=self.warehouse)

        # A deducts B by 2, then B tries to deduct A but A is in _seen → stops
        self.assertEqual(self._stock(b), 98)  # 100 - (1×2) = 98

    def test_component_uses_own_warehouse(self):
        """When a component has its own default_warehouse, use that instead."""
        wh2 = Warehouse.objects.create(code="WH2", name="Secondary", company=self.company)
        parent = self._make_item("P-WH", stock=10)
        comp = self._make_item("C-WH", stock=50, default_warehouse=wh2)

        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        parent.deduct_bom_components(quantity=3, warehouse=self.warehouse)

        # Transaction should be in WH2 (component's warehouse), not WH1
        tx = ItemTransaction.objects.filter(item=comp).first()
        self.assertEqual(tx.warehouse, wh2)

    def test_reference_object_propagated(self):
        """The reference_object is forwarded to component transactions."""
        parent = self._make_item("P-REF", stock=10)
        comp = self._make_item("C-REF", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        # Use the parent item itself as a dummy reference object
        parent.deduct_bom_components(
            quantity=1,
            warehouse=self.warehouse,
            reference_object=parent,
        )

        tx = ItemTransaction.objects.filter(item=comp).first()
        self.assertIsNotNone(tx.reference_content_type)
        self.assertEqual(tx.reference_object_id, parent.pk)

    def test_movement_type_is_out(self):
        """All BOM deduction transactions must be MovementType.OUT."""
        parent = self._make_item("P-MT", stock=10)
        comp = self._make_item("C-MT", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        parent.deduct_bom_components(quantity=1, warehouse=self.warehouse)

        tx = ItemTransaction.objects.filter(item=comp).first()
        self.assertEqual(tx.movement_type, MovementType.OUT)
        self.assertEqual(tx.quantity_change, -2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. AdjustStockView cascade
# ─────────────────────────────────────────────────────────────────────────────

class AdjustStockBOMCascadeTest(_BOMBaseTestCase):
    """Test that the Adjust Stock admin view cascades BOM on negatives."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        # Give user full permissions
        from django.contrib.auth.models import Permission
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()

    def test_negative_adjustment_cascades_bom(self):
        parent = self._make_item("ADJ-P", stock=20)
        comp = self._make_item("ADJ-C", stock=100)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=5)

        url = reverse("admin:inventory_item_adjust", args=[parent.pk])
        response = self.client.post(url, {
            "quantity_change": -3,
            "warehouse": self.warehouse.pk,
            "note": "Damaged",
        })

        # Should redirect on success
        self.assertEqual(response.status_code, 302)

        # Parent: 20 - 3 = 17
        self.assertEqual(self._stock(parent), 17)
        # Component: 100 - (5×3) = 85
        self.assertEqual(self._stock(comp), 85)

    def test_positive_adjustment_does_not_cascade(self):
        parent = self._make_item("ADJ-P2", stock=10)
        comp = self._make_item("ADJ-C2", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=3)

        url = reverse("admin:inventory_item_adjust", args=[parent.pk])
        response = self.client.post(url, {
            "quantity_change": 5,
            "warehouse": self.warehouse.pk,
            "note": "Found extra",
        })

        self.assertEqual(response.status_code, 302)

        # Parent: 10 + 5 = 15
        self.assertEqual(self._stock(parent), 15)
        # Component: unchanged at 50
        self.assertEqual(self._stock(comp), 50)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Delivery order stock-out cascade
# ─────────────────────────────────────────────────────────────────────────────

class _DeliveryOrderStockTestCase(_BOMBaseTestCase):
    """Fixtures and helpers for delivering quoted inventory items."""

    def setUp(self):
        super().setUp()
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF BOM",
            company=self.company,
            client=self.client_obj,
        )
        self.inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

    def _accepted_quote_line(self, item, quantity):
        quote = Quote.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            project_name="BOM Test Quote",
            status=QuoteStatus.ACCEPTED,
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
                "source_content_type_id": self.inv_item_ct.id,
                "source_object_id": quote_line.pk,
                "description": quote_line.inventory_item.name,
                "quantity": quantity if quantity is not None else quote_line.quantity,
                "unit_price": quote_line.unit_price.amount,
            }],
        )


class DeliveryOrderBOMCascadeTest(_DeliveryOrderStockTestCase):
    """Stock leaves when items are put on a delivery order, not when the
    quote is accepted."""

    def test_quote_acceptance_does_not_move_stock(self):
        parent = self._make_item("Q-NOOP", stock=50)
        comp = self._make_item("Q-NOOP-C", stock=200)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=3)

        tx_count_before = ItemTransaction.objects.count()
        self._accepted_quote_line(parent, "4")

        self.assertEqual(ItemTransaction.objects.count(), tx_count_before)
        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)

    def test_delivery_order_deducts_bom_components(self):
        parent = self._make_item("Q-P", stock=50)
        comp_a = self._make_item("Q-CA", stock=200)
        comp_b = self._make_item("Q-CB", stock=100)

        BillOfMaterialsLine.objects.create(parent=parent, component=comp_a, quantity=3)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_b, quantity=1)

        line = self._accepted_quote_line(parent, "4")
        self._deliver(line)

        # Parent: 50 - 4 = 46
        self.assertEqual(self._stock(parent), 46)
        # comp_a: 200 - (3×4) = 188
        self.assertEqual(self._stock(comp_a), 188)
        # comp_b: 100 - (1×4) = 96
        self.assertEqual(self._stock(comp_b), 96)

    def test_stock_out_references_the_delivery_order(self):
        parent = self._make_item("Q-REF", stock=10)
        line = self._accepted_quote_line(parent, "2")
        do = self._deliver(line)

        tx = ItemTransaction.objects.filter(
            item=parent, movement_type=MovementType.OUT
        ).get()
        self.assertEqual(tx.quantity_change, -2)
        self.assertEqual(tx.reference_object, do)

    def test_service_items_not_cascaded(self):
        """Service-type items should not trigger BOM deduction (or any deduction)."""
        service = self._make_item(
            "SRV-1", "Consulting",
            item_type=ItemType.SERVICE,
            default_warehouse=None,
        )

        line = self._accepted_quote_line(service, "1")
        tx_count_before = ItemTransaction.objects.count()
        self._deliver(line)

        # No transactions should have been created for a service item
        self.assertEqual(ItemTransaction.objects.count(), tx_count_before)

    def test_nested_bom_on_delivery(self):
        """Nested BOMs are fully deducted when the delivery order is created."""
        raw = self._make_item("Q-RAW", stock=500)
        sub = self._make_item("Q-SUB", stock=100)
        top = self._make_item("Q-TOP", stock=30)

        BillOfMaterialsLine.objects.create(parent=sub, component=raw, quantity=10)
        BillOfMaterialsLine.objects.create(parent=top, component=sub, quantity=2)

        line = self._accepted_quote_line(top, "5")
        self._deliver(line)

        # top: 30 - 5 = 25
        self.assertEqual(self._stock(top), 25)
        # sub: 100 - (2×5) = 90
        self.assertEqual(self._stock(sub), 90)
        # raw: 500 - (10 × (2×5)) = 500 - 100 = 400
        self.assertEqual(self._stock(raw), 400)

    def test_partial_delivery_deducts_only_what_is_delivered(self):
        parent = self._make_item("Q-PART", stock=20)
        comp = self._make_item("Q-PART-C", stock=100)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        line = self._accepted_quote_line(parent, "10")

        self._deliver(line, quantity=Decimal("4"))
        # Parent: 20 - 4 = 16, comp: 100 - (2×4) = 92
        self.assertEqual(self._stock(parent), 16)
        self.assertEqual(self._stock(comp), 92)

        self._deliver(line, quantity=Decimal("6"))
        # Parent: 16 - 6 = 10, comp: 92 - (2×6) = 80
        self.assertEqual(self._stock(parent), 10)
        self.assertEqual(self._stock(comp), 80)

    def test_configuration_lines_do_not_move_stock(self):
        """Configuration lines are produced, not taken out of inventory."""
        from configurator.models import Configuration, ConfigurationStatus, ProductTemplate

        product = ProductTemplate.objects.create(name="Door", company=self.company)
        quote = Quote.objects.create(
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            project_name="Config Quote",
            status=QuoteStatus.ACCEPTED,
        )
        config = Configuration.objects.create(
            name="Config",
            company=self.company,
            product=product,
            client=self.client_obj,
            quote=quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=Decimal("100"),
            total_price=Decimal("121"),
        )

        tx_count_before = ItemTransaction.objects.count()
        DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=[{
                "source_content_type_id": ContentType.objects.get_for_model(Configuration).id,
                "source_object_id": config.pk,
                "description": config.name,
                "quantity": Decimal("1"),
                "unit_price": Decimal("100"),
            }],
        )

        self.assertEqual(ItemTransaction.objects.count(), tx_count_before)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Delivery order cancellation / deletion restock
# ─────────────────────────────────────────────────────────────────────────────

class DeliveryOrderRestockTest(_DeliveryOrderStockTestCase):
    """Cancelling or deleting a delivery order gives the goods back."""

    def _delivered(self, parent_stock=50, comp_stock=200, bom_qty=3, delivered=4):
        """Deliver `delivered` units of a BOM parent; return (do, parent, comp)."""
        parent = self._make_item("R-P", stock=parent_stock)
        comp = self._make_item("R-C", stock=comp_stock)
        BillOfMaterialsLine.objects.create(
            parent=parent, component=comp, quantity=bom_qty,
        )
        line = self._accepted_quote_line(parent, str(delivered))
        return self._deliver(line), parent, comp

    def test_cancelling_returns_stock_and_bom_components(self):
        do, parent, comp = self._delivered()
        self.assertEqual(self._stock(parent), 46)
        self.assertEqual(self._stock(comp), 188)

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)

    def test_return_is_a_compensating_movement_not_a_deletion(self):
        do, parent, _ = self._delivered()

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        out_tx = ItemTransaction.objects.get(
            item=parent, movement_type=MovementType.OUT,
        )
        in_tx = ItemTransaction.objects.filter(
            item=parent, movement_type=MovementType.IN,
        ).exclude(note="Initial stock").get()
        self.assertEqual(out_tx.quantity_change, -4)
        self.assertEqual(in_tx.quantity_change, 4)
        self.assertEqual(in_tx.reference_object, do)
        self.assertIn("cancelled", in_tx.note)

    def test_saving_a_cancelled_order_again_does_not_return_twice(self):
        do, parent, comp = self._delivered()

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()
        do.notes = "Cancelled by mistake in the notes"
        do.save()
        DeliveryOrder.objects.get(pk=do.pk).save()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)

    def test_reopening_a_cancelled_order_takes_the_stock_out_again(self):
        do, parent, comp = self._delivered()

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()
        self.assertEqual(self._stock(parent), 50)

        do.status = DeliveryOrderStatus.SENT
        do.save()

        self.assertEqual(self._stock(parent), 46)
        self.assertEqual(self._stock(comp), 188)

    def test_deleting_the_order_returns_stock(self):
        do, parent, comp = self._delivered()
        number = do.number

        do.delete()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)
        # The return is not left pointing at the row that just disappeared.
        in_tx = ItemTransaction.objects.filter(
            item=parent, movement_type=MovementType.IN,
        ).exclude(note="Initial stock").get()
        self.assertIsNone(in_tx.reference_object)
        self.assertIn(number, in_tx.note)

    def test_bulk_deleting_orders_returns_stock(self):
        do, parent, comp = self._delivered()

        DeliveryOrder.objects.filter(pk=do.pk).delete()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)

    def test_deleting_a_cancelled_order_does_not_return_twice(self):
        do, parent, comp = self._delivered()

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()
        do.delete()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)

    def test_deleting_one_line_returns_only_that_line(self):
        parent_a = self._make_item("R-A", stock=20)
        parent_b = self._make_item("R-B", stock=30)
        line_a = self._accepted_quote_line(parent_a, "5")
        line_b = self._accepted_quote_line(parent_b, "7")

        do = DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=[
                {
                    "source_content_type_id": self.inv_item_ct.id,
                    "source_object_id": line_a.pk,
                    "description": parent_a.name,
                    "quantity": line_a.quantity,
                    "unit_price": Decimal("100"),
                },
                {
                    "source_content_type_id": self.inv_item_ct.id,
                    "source_object_id": line_b.pk,
                    "description": parent_b.name,
                    "quantity": line_b.quantity,
                    "unit_price": Decimal("100"),
                },
            ],
        )
        self.assertEqual(self._stock(parent_a), 15)
        self.assertEqual(self._stock(parent_b), 23)

        do.items.filter(source_object_id=line_a.pk).first().delete()

        self.assertEqual(self._stock(parent_a), 20)
        self.assertEqual(self._stock(parent_b), 23)
        # The order survives, so the return keeps its link to it.
        in_tx = ItemTransaction.objects.filter(
            item=parent_a, movement_type=MovementType.IN,
        ).exclude(note="Initial stock").get()
        self.assertEqual(in_tx.reference_object, do)

    def test_cancelling_an_order_with_no_stockable_lines_is_a_noop(self):
        service = self._make_item(
            "R-SRV", "Consulting",
            item_type=ItemType.SERVICE,
            default_warehouse=None,
        )
        do = self._deliver(self._accepted_quote_line(service, "1"))

        tx_count_before = ItemTransaction.objects.count()
        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        self.assertEqual(ItemTransaction.objects.count(), tx_count_before)

    def test_adding_a_line_to_a_cancelled_order_does_not_move_stock(self):
        do, parent, comp = self._delivered()

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        extra = self._make_item("R-EXTRA", stock=8)
        extra_line = self._accepted_quote_line(extra, "3")
        DeliveryOrderItem.objects.create(
            delivery_order=do,
            description=extra.name,
            quantity=extra_line.quantity,
            unit_price=Decimal("100"),
            source_content_type=self.inv_item_ct,
            source_object_id=extra_line.pk,
        )

        self.assertEqual(self._stock(extra), 8)
        self.assertEqual(self._stock(parent), 50)
