"""
Tests for the Bill of Materials (BOM) feature.

A composite item is a recipe rather than goods on a shelf, so it never moves
stock of its own — only the components it is built from do, at every level of
nesting.

Covers:
  • BillOfMaterialsLine model constraints & validation
  • InventoryItem.deduct_bom_components() — flat, nested, circular
  • Low-stock alerts and the changelist filter skipping composites
  • AdjustStockView, ReceiveStockView and TransferStockView moving components
  • Purchase order receipts crediting components rather than the parent
  • DeliveryOrderItem._deduct_inventory() cascade on delivery
  • Restock on delivery order cancellation / deletion
  • The deploy migration that brings existing composites to zero
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import translation

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

    def _fresh(self, item):
        """Re-read an item: ``has_components`` is written by a signal, so an
        instance held across a bill-of-materials edit has a stale copy."""
        return InventoryItem.objects.get(pk=item.pk)


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
        """Parent → Sub-assembly → Raw part: the walk reaches the bottom, and
        only the raw part — the one actually on a shelf — moves."""
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

        # The sub-assembly is a recipe of its own, so it stays put.
        self.assertEqual(self._stock(sub_assy), 50)
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

        # Every item in a cycle is composite, so the walk reaches no goods:
        # A skips B as a recipe, recurses into B, and B stops on A via _seen.
        self.assertEqual(self._stock(a), 100)
        self.assertEqual(self._stock(b), 100)

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
    """Adjusting a composite corrects the parts it is built from, in both
    directions, and never its own figure."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        # Give user full permissions
        from django.contrib.auth.models import Permission
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()

    def _adjust(self, item, quantity_change, note="Adjusted"):
        response = self.client.post(
            reverse("admin:inventory_item_adjust", args=[item.pk]),
            {
                "quantity_change": quantity_change,
                "warehouse": self.warehouse.pk,
                "note": note,
            },
        )
        self.assertEqual(response.status_code, 302)  # redirects on success
        return response

    def test_negative_adjustment_moves_the_components_not_the_composite(self):
        parent = self._make_item("ADJ-P", stock=20)
        comp = self._make_item("ADJ-C", stock=100)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=5)

        self._adjust(parent, -3, note="Damaged")

        self.assertEqual(self._stock(parent), 20)      # a recipe holds no goods
        self.assertEqual(self._stock(comp), 85)        # 100 - (5×3)

    def test_positive_adjustment_puts_the_components_back(self):
        parent = self._make_item("ADJ-P2", stock=10)
        comp = self._make_item("ADJ-C2", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=3)

        self._adjust(parent, 5, note="Found extra")

        self.assertEqual(self._stock(parent), 10)
        self.assertEqual(self._stock(comp), 65)        # 50 + (3×5)

    def test_an_adjustment_and_its_opposite_cancel_out(self):
        parent = self._make_item("ADJ-P3", stock=10)
        comp = self._make_item("ADJ-C3", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=4)

        self._adjust(parent, -3, note="Miscounted")
        self._adjust(parent, 3, note="Undo")

        self.assertEqual(self._stock(parent), 10)
        self.assertEqual(self._stock(comp), 50)

    def test_a_plain_item_still_adjusts_its_own_stock(self):
        item = self._make_item("ADJ-PLAIN", stock=20)

        self._adjust(item, -3)

        self.assertEqual(self._stock(item), 17)

    def test_the_form_warns_and_drops_the_own_stock_preview_for_a_composite(self):
        parent = self._make_item("ADJ-FORM", stock=10)
        comp = self._make_item("ADJ-FORM-C", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        with translation.override("en"):
            page = self.client.get(reverse("admin:inventory_item_adjust", args=[parent.pk]))
            plain = self.client.get(
                reverse("admin:inventory_item_adjust", args=[comp.pk])
            )

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "holds no stock of its own")
        # Projecting the item's own figure would be misleading, so the panel
        # is gone (the script still refers to it by id, hence matching markup).
        self.assertNotContains(page, 'id="adj-after-preview"')
        self.assertContains(plain, 'id="adj-after-preview"')


# ─────────────────────────────────────────────────────────────────────────────
# 3. has_components: derived from the bill of materials, never set by hand
# ─────────────────────────────────────────────────────────────────────────────

class CompositeFlagTest(_BOMBaseTestCase):
    """``has_components`` is what every stock path reads, so it has to follow
    the lines exactly — a flag left behind would move stock the wrong way."""

    def test_adding_a_line_marks_the_parent_only(self):
        parent = self._make_item("FLG-P")
        comp = self._make_item("FLG-C")

        self.assertFalse(self._fresh(parent).is_composite)

        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        self.assertTrue(self._fresh(parent).is_composite)
        self.assertFalse(self._fresh(comp).is_composite)

    def test_removing_the_last_line_clears_it(self):
        parent = self._make_item("FLG-P2")
        comp = self._make_item("FLG-C2")
        line = BillOfMaterialsLine.objects.create(
            parent=parent, component=comp, quantity=1,
        )

        line.delete()

        self.assertFalse(self._fresh(parent).is_composite)

    def test_removing_one_of_several_keeps_it(self):
        parent = self._make_item("FLG-P3")
        first = BillOfMaterialsLine.objects.create(
            parent=parent, component=self._make_item("FLG-C3A"), quantity=1,
        )
        BillOfMaterialsLine.objects.create(
            parent=parent, component=self._make_item("FLG-C3B"), quantity=1,
        )

        first.delete()

        self.assertTrue(self._fresh(parent).is_composite)

    def test_moving_a_line_to_another_parent_updates_both(self):
        old_parent = self._make_item("FLG-OLD")
        new_parent = self._make_item("FLG-NEW")
        comp = self._make_item("FLG-C4")
        line = BillOfMaterialsLine.objects.create(
            parent=old_parent, component=comp, quantity=1,
        )

        line.parent = new_parent
        line.save()

        self.assertFalse(self._fresh(old_parent).is_composite)
        self.assertTrue(self._fresh(new_parent).is_composite)

    def test_deleting_the_parent_takes_its_lines_with_it(self):
        parent = self._make_item("FLG-P5")
        comp = self._make_item("FLG-C5")
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        parent.delete()

        self.assertFalse(self._fresh(comp).is_composite)


# ─────────────────────────────────────────────────────────────────────────────
# 3a. Low-stock alerts skip composites
# ─────────────────────────────────────────────────────────────────────────────

class CompositeLowStockTest(_BOMBaseTestCase):
    """A composite has no stock of its own, so a minimum threshold on it can
    never be breached — otherwise every composite would alert forever."""

    def _with_minimum(self, code, minimum, stock=0):
        item = self._make_item(code, stock=stock)
        item.min_stock = minimum
        item.save()
        return item

    def test_a_composite_never_reads_as_low_stock(self):
        parent = self._with_minimum("LOW-P", minimum=10)
        comp = self._make_item("LOW-C", stock=500)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        # Sitting at 0 against a minimum of 10 would trip the plain rule.
        self.assertEqual(self._stock(parent), 0)
        self.assertFalse(InventoryItem.objects.get(pk=parent.pk).is_low_stock)

    def test_a_plain_item_below_its_minimum_still_reads_as_low(self):
        item = self._with_minimum("LOW-PLAIN", minimum=10, stock=4)
        self.assertTrue(InventoryItem.objects.get(pk=item.pk).is_low_stock)

    def test_a_component_below_its_minimum_still_reads_as_low(self):
        parent = self._make_item("LOW-P2")
        comp = self._with_minimum("LOW-C2", minimum=50, stock=5)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        self.assertTrue(InventoryItem.objects.get(pk=comp.pk).is_low_stock)



class LowStockFilterTest(_BOMBaseTestCase):
    """The changelist filter follows the same rule as the badge, or the list
    would contradict the row it shows."""

    def setUp(self):
        super().setUp()
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)

    def _codes_for(self, stock_level):
        response = self.client.get(
            reverse("admin:inventory_inventoryitem_changelist"),
            {"stock_level": stock_level},
        )
        self.assertEqual(response.status_code, 200)
        return {i.code for i in response.context["cl"].queryset}

    def test_a_composite_is_not_listed_as_low_stock(self):
        parent = self._make_item("FLT-P")
        parent.min_stock = 10
        parent.save()
        comp = self._make_item("FLT-C", stock=2)
        comp.min_stock = 50
        comp.save()
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        low = self._codes_for("low")

        self.assertIn("FLT-C", low)        # a real shortage
        self.assertNotIn("FLT-P", low)     # a recipe cannot run short

    def test_a_composite_falls_under_stock_ok(self):
        """Low and OK still add up to the whole list, so nothing disappears."""
        parent = self._make_item("FLT-P2")
        parent.min_stock = 10
        parent.save()
        comp = self._make_item("FLT-C2", stock=2)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=1)

        self.assertIn("FLT-P2", self._codes_for("ok"))


# ─────────────────────────────────────────────────────────────────────────────
# 3b. Receive / Transfer: a composite stands in for the goods it is built from
# ─────────────────────────────────────────────────────────────────────────────

class _MovementScreenTestCase(_BOMBaseTestCase):
    """Logged-in admin plus per-warehouse stock reads."""

    def setUp(self):
        super().setUp()
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)

    def _stock_in(self, item, warehouse):
        total = ItemTransaction.objects.filter(
            item=item, warehouse=warehouse,
        ).aggregate(t=models.Sum("quantity_change"))["t"]
        return total or 0


class ReceiveStockBOMCascadeTest(_MovementScreenTestCase):
    """Receiving a composite brings in its components, never the item itself."""

    def _receive(self, item, quantity, warehouse=None, note="Received"):
        response = self.client.post(
            reverse("admin:inventory_item_receive", args=[item.pk]),
            {
                "quantity": quantity,
                "warehouse": (warehouse or self.warehouse).pk,
                "received_date": "2026-01-15",
                "note": note,
            },
        )
        self.assertEqual(response.status_code, 302)
        return response

    def test_receiving_a_composite_brings_in_its_components(self):
        parent = self._make_item("RCV-P", stock=0)
        comp_a = self._make_item("RCV-A", stock=10)
        comp_b = self._make_item("RCV-B", stock=5)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_a, quantity=2)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_b, quantity=1)

        self._receive(parent, 3)

        self.assertEqual(self._stock(parent), 0)       # the recipe stays empty
        self.assertEqual(self._stock(comp_a), 16)      # 10 + (2×3)
        self.assertEqual(self._stock(comp_b), 8)       # 5 + (1×3)

    def test_receiving_reaches_the_leaves_of_a_nested_bom(self):
        top = self._make_item("RCV-TOP")
        sub = self._make_item("RCV-SUB")
        leaf = self._make_item("RCV-LEAF", stock=100)
        BillOfMaterialsLine.objects.create(parent=top, component=sub, quantity=2)
        BillOfMaterialsLine.objects.create(parent=sub, component=leaf, quantity=4)

        self._receive(top, 5)

        self.assertEqual(self._stock(top), 0)
        self.assertEqual(self._stock(sub), 0)          # composite too
        self.assertEqual(self._stock(leaf), 140)       # 100 + (4 × 2×5)

    def test_receiving_a_plain_item_is_unchanged(self):
        item = self._make_item("RCV-PLAIN", stock=10)

        self._receive(item, 7)

        self.assertEqual(self._stock(item), 17)

    def test_the_form_warns_and_drops_the_own_stock_preview_for_a_composite(self):
        parent = self._make_item("RCV-FORM")
        comp = self._make_item("RCV-FORM-C", stock=5)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        with translation.override("en"):
            page = self.client.get(reverse("admin:inventory_item_receive", args=[parent.pk]))
            plain = self.client.get(reverse("admin:inventory_item_receive", args=[comp.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "holds no stock of its own")
        self.assertNotContains(page, 'id="qty-after"')
        self.assertContains(plain, 'id="qty-after"')


class TransferStockBOMCascadeTest(_MovementScreenTestCase):
    """Transferring a composite moves its components between the two chosen
    warehouses."""

    def setUp(self):
        super().setUp()
        self.wh2 = Warehouse.objects.create(
            code="WH2", name="Secondary", company=self.company,
        )

    def _transfer(self, item, quantity, source, destination, note="Moved"):
        response = self.client.post(
            reverse("admin:inventory_item_transfer", args=[item.pk]),
            {
                "quantity": quantity,
                "source_warehouse": source.pk,
                "destination_warehouse": destination.pk,
                "note": note,
            },
        )
        self.assertEqual(response.status_code, 302)
        return response

    def test_transferring_a_composite_moves_its_components(self):
        parent = self._make_item("TRF-P")
        comp = self._make_item("TRF-C", stock=50)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=4)

        self._transfer(parent, 3, self.warehouse, self.wh2)

        # The total is untouched; only where the goods sit changed.
        self.assertEqual(self._stock(parent), 0)
        self.assertEqual(self._stock(comp), 50)
        self.assertEqual(self._stock_in(comp, self.warehouse), 38)   # 50 - (4×3)
        self.assertEqual(self._stock_in(comp, self.wh2), 12)         # 4×3

    def test_a_components_own_warehouse_does_not_override_the_chosen_ones(self):
        """Why the transfer pins the warehouse: a component that has a default
        warehouse of its own would otherwise pull both legs of the move into
        that one place, and the transfer would go through having moved
        nothing."""
        parent = self._make_item("TRF-P2")
        comp = self._make_item(
            "TRF-C2", stock=20, default_warehouse=self.warehouse,
        )
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        self._transfer(parent, 5, self.warehouse, self.wh2)

        self.assertEqual(self._stock_in(comp, self.warehouse), 10)   # 20 - (2×5)
        self.assertEqual(self._stock_in(comp, self.wh2), 10)         # 2×5

    def test_transferring_a_plain_item_is_unchanged(self):
        item = self._make_item("TRF-PLAIN", stock=30)

        self._transfer(item, 12, self.warehouse, self.wh2)

        self.assertEqual(self._stock(item), 30)
        self.assertEqual(self._stock_in(item, self.warehouse), 18)
        self.assertEqual(self._stock_in(item, self.wh2), 12)

    def test_the_form_warns_for_a_composite(self):
        parent = self._make_item("TRF-FORM")
        comp = self._make_item("TRF-FORM-C", stock=5)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        with translation.override("en"):
            page = self.client.get(reverse("admin:inventory_item_transfer", args=[parent.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "holds no stock of its own")


# ─────────────────────────────────────────────────────────────────────────────
# 3c. Purchase order receipts follow the same rule
# ─────────────────────────────────────────────────────────────────────────────

class PurchaseReceiptBOMCascadeTest(_BOMBaseTestCase):
    """Goods arriving against a purchase order are the same physical event as
    the Receive screen, so a composite has to behave the same way: crediting
    the parent would trap stock that delivery can never take out again."""

    def setUp(self):
        super().setUp()
        self.provider = Provider.objects.create(name="Supplier", company=self.company)

    def _receive(self, item, quantity):
        order = PurchaseOrder.objects.create(
            company=self.company,
            provider=self.provider,
            po_type=PurchaseOrderType.STOCK,
            status=PurchaseOrderStatus.APPROVED,
        )
        po_item = PurchaseOrderItem.objects.create(
            purchase_order=order,
            inventory_item=item,
            quantity=Decimal(str(quantity)),
            unit_price=Decimal("10"),
        )
        receipt = PurchaseOrderReceipt.objects.create(
            company=self.company, purchase_order=order, warehouse=self.warehouse,
        )
        return PurchaseOrderReceiptItem.objects.create(
            receipt=receipt, po_item=po_item, quantity_received=Decimal(str(quantity)),
        )

    def test_receiving_a_composite_credits_its_components(self):
        parent = self._make_item("PO-P")
        comp_a = self._make_item("PO-A", stock=10)
        comp_b = self._make_item("PO-B", stock=5)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_a, quantity=2)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_b, quantity=1)

        self._receive(self._fresh(parent), 3)

        self.assertEqual(self._stock(parent), 0)       # the recipe holds nothing
        self.assertEqual(self._stock(comp_a), 16)      # 10 + (2×3)
        self.assertEqual(self._stock(comp_b), 8)       # 5 + (1×3)

    def test_receiving_reaches_the_leaves_of_a_nested_bom(self):
        top = self._make_item("PO-TOP")
        sub = self._make_item("PO-SUB")
        leaf = self._make_item("PO-LEAF", stock=100)
        BillOfMaterialsLine.objects.create(parent=top, component=sub, quantity=2)
        BillOfMaterialsLine.objects.create(parent=sub, component=leaf, quantity=4)

        self._receive(self._fresh(top), 5)

        self.assertEqual(self._stock(top), 0)
        self.assertEqual(self._stock(sub), 0)          # a recipe of its own
        self.assertEqual(self._stock(leaf), 140)       # 100 + (4 × 2×5)

    def test_receiving_a_plain_item_is_unchanged(self):
        item = self._make_item("PO-PLAIN", stock=10)

        self._receive(item, 7)

        self.assertEqual(self._stock(item), 17)

    def test_a_composite_never_traps_what_a_delivery_cannot_return(self):
        """Received then shipped: everything that came in goes back out, with
        nothing stranded on the parent."""
        parent = self._make_item("PO-CYCLE-P")
        comp = self._make_item("PO-CYCLE-C")
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        self._receive(self._fresh(parent), 4)
        self.assertEqual(self._stock(comp), 8)

        self._fresh(parent).deduct_bom_components(
            quantity=4, warehouse=self.warehouse,
        )

        self.assertEqual(self._stock(parent), 0)
        self.assertEqual(self._stock(comp), 0)


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

        # The parent is a recipe: its own figure stays put.
        self.assertEqual(self._stock(parent), 50)
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

    def test_nested_bom_deducts_only_the_leaves(self):
        """A nested BOM is walked to the bottom, and only the items that are
        not themselves assembled from something else move."""
        raw = self._make_item("Q-RAW", stock=500)
        sub = self._make_item("Q-SUB", stock=100)
        top = self._make_item("Q-TOP", stock=30)

        BillOfMaterialsLine.objects.create(parent=sub, component=raw, quantity=10)
        BillOfMaterialsLine.objects.create(parent=top, component=sub, quantity=2)

        line = self._accepted_quote_line(top, "5")
        self._deliver(line)

        # top and sub are both recipes, so neither moves.
        self.assertEqual(self._stock(top), 30)
        self.assertEqual(self._stock(sub), 100)
        # raw: 500 - (10 × (2×5)) = 500 - 100 = 400
        self.assertEqual(self._stock(raw), 400)

    def test_partial_delivery_deducts_only_what_is_delivered(self):
        parent = self._make_item("Q-PART", stock=20)
        comp = self._make_item("Q-PART-C", stock=100)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp, quantity=2)

        line = self._accepted_quote_line(parent, "10")

        self._deliver(line, quantity=Decimal("4"))
        # The parent is a recipe; comp: 100 - (2×4) = 92
        self.assertEqual(self._stock(parent), 20)
        self.assertEqual(self._stock(comp), 92)

        self._deliver(line, quantity=Decimal("6"))
        # comp: 92 - (2×6) = 80
        self.assertEqual(self._stock(parent), 20)
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
        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 188)

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)

    def test_return_is_a_compensating_movement_not_a_deletion(self):
        do, parent, comp = self._delivered()

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        # The recipe itself never moved, so it has nothing to compensate.
        self.assertFalse(
            ItemTransaction.objects.filter(item=parent)
            .exclude(note="Initial stock").exists()
        )

        out_tx = ItemTransaction.objects.get(
            item=comp, movement_type=MovementType.OUT,
        )
        in_tx = ItemTransaction.objects.filter(
            item=comp, movement_type=MovementType.IN,
        ).exclude(note="Initial stock").get()
        self.assertEqual(out_tx.quantity_change, -12)   # 3×4
        self.assertEqual(in_tx.quantity_change, 12)
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

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 188)

    def test_deleting_the_order_returns_stock(self):
        do, parent, comp = self._delivered()
        number = do.number

        do.delete()

        self.assertEqual(self._stock(parent), 50)
        self.assertEqual(self._stock(comp), 200)
        # The return is not left pointing at the row that just disappeared.
        in_tx = ItemTransaction.objects.filter(
            item=comp, movement_type=MovementType.IN,
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


# ─────────────────────────────────────────────────────────────────────────────
# 6. The deploy migration: composites brought to zero
# ─────────────────────────────────────────────────────────────────────────────

class ZeroOutCompositeStockMigrationTest(_DeliveryOrderStockTestCase):
    """Orders delivered under the old rule left the parent deducted for good.

    The migration closes that gap before the new return logic can ever run
    against them, so exercise it against data shaped the way the old rule
    left it.
    """

    def _run_migration(self):
        """Call the migration's own function — its module name starts with a
        digit, so it cannot be imported with a plain ``import``."""
        from importlib import import_module

        from django.apps import apps as real_apps

        module = import_module("inventory.migrations.0029_zero_out_composite_stock")
        module.zero_out_composite_stock(real_apps, None)

    def _kit(self):
        """Ventana = 2 × Marco + 1 × Manilla ; Marco = 4 × Perfil."""
        ventana = self._make_item("M-VEN", stock=10)
        marco = self._make_item("M-MAR", stock=100)
        manilla = self._make_item("M-MAN", stock=50)
        perfil = self._make_item("M-PER", stock=1000)
        BillOfMaterialsLine.objects.create(parent=ventana, component=marco, quantity=2)
        BillOfMaterialsLine.objects.create(parent=ventana, component=manilla, quantity=1)
        BillOfMaterialsLine.objects.create(parent=marco, component=perfil, quantity=4)
        return ventana, marco, manilla, perfil

    def _order_delivered_by_the_old_rule(self, ventana, quantity=3):
        """An order whose ledger looks the way the previous rule wrote it:
        the parent and every component taken out, the line flagged."""
        order = DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=DeliveryOrderStatus.SENT,
        )
        line = DeliveryOrderItem.objects.create(
            delivery_order=order, inventory_item=ventana,
            quantity=Decimal(str(quantity)),
        )
        # Drop what today's rule just wrote, then lay down the old shape.
        ItemTransaction.objects.filter(
            reference_content_type=ContentType.objects.get_for_model(order),
            reference_object_id=order.pk,
        ).delete()
        for code, qty in (
            ("M-VEN", quantity), ("M-MAR", 2 * quantity),
            ("M-MAN", quantity), ("M-PER", 8 * quantity),
        ):
            ItemTransaction.objects.create(
                item=InventoryItem.objects.get(code=code), warehouse=self.warehouse,
                movement_type=MovementType.OUT, quantity_change=-qty,
                reference_object=order, note="delivered under the old rule",
            )
        DeliveryOrderItem.objects.filter(pk=line.pk).update(stock_deducted=True)
        return order

    def test_it_zeroes_composites_and_leaves_the_components_alone(self):
        ventana, marco, manilla, perfil = self._kit()
        self._order_delivered_by_the_old_rule(ventana)

        self._run_migration()

        self.assertEqual(self._stock(ventana), 0)      # recipe
        self.assertEqual(self._stock(marco), 0)        # recipe too
        self.assertEqual(self._stock(manilla), 47)     # real goods, untouched
        self.assertEqual(self._stock(perfil), 976)

    def test_cancelling_an_old_order_afterwards_leaks_nothing(self):
        """The point of the whole migration: the components come back and the
        recipes stay at zero, instead of keeping a phantom deficit."""
        ventana, marco, manilla, perfil = self._kit()
        order = self._order_delivered_by_the_old_rule(ventana)
        self._run_migration()

        order.status = DeliveryOrderStatus.CANCELLED
        order.save()

        self.assertEqual(self._stock(ventana), 0)
        self.assertEqual(self._stock(marco), 0)
        self.assertEqual(self._stock(manilla), 50)     # whole again
        self.assertEqual(self._stock(perfil), 1000)

    def test_it_balances_each_warehouse_on_its_own(self):
        wh2 = Warehouse.objects.create(code="M-WH2", name="Second", company=self.company)
        ventana, *_ = self._kit()
        ItemTransaction.objects.create(
            item=ventana, warehouse=wh2, movement_type=MovementType.OUT,
            quantity_change=-7, note="old rule, other warehouse",
        )

        self._run_migration()

        by_wh = {
            wh.pk: ItemTransaction.objects.filter(item=ventana, warehouse=wh)
            .aggregate(t=models.Sum("quantity_change"))["t"] or 0
            for wh in (self.warehouse, wh2)
        }
        self.assertEqual(by_wh[self.warehouse.pk], 0)
        self.assertEqual(by_wh[wh2.pk], 0)

    def test_it_leaves_a_plain_item_untouched(self):
        plain = self._make_item("M-PLAIN", stock=25)

        self._run_migration()

        self.assertEqual(self._stock(plain), 25)

    def test_it_does_nothing_when_there_are_no_composites(self):
        plain = self._make_item("M-NONE", stock=5)
        before = ItemTransaction.objects.count()

        self._run_migration()

        self.assertEqual(ItemTransaction.objects.count(), before)
        self.assertEqual(self._stock(plain), 5)
