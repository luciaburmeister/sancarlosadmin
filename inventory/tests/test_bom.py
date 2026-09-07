"""
Tests for the Bill of Materials (BOM) feature.

Covers:
  • BillOfMaterialsLine model constraints & validation
  • InventoryItem.deduct_bom_components() — flat, nested, circular
  • AdjustStockView cascade on negative adjustments
  • Quote._deduct_inventory() cascade on acceptance
"""

from decimal import Decimal

from django.contrib.auth.models import User
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
# 4. Quote acceptance cascade
# ─────────────────────────────────────────────────────────────────────────────

class QuoteAcceptanceBOMCascadeTest(_BOMBaseTestCase):
    """Test that accepting a Quote cascades BOM deductions."""

    def test_quote_acceptance_deducts_bom_components(self):
        from sales.models import Quote, QuoteInventoryItem, QuoteStatus

        parent = self._make_item("Q-P", stock=50)
        comp_a = self._make_item("Q-CA", stock=200)
        comp_b = self._make_item("Q-CB", stock=100)

        BillOfMaterialsLine.objects.create(parent=parent, component=comp_a, quantity=3)
        BillOfMaterialsLine.objects.create(parent=parent, component=comp_b, quantity=1)

        # Create a draft quote
        quote = Quote.objects.create(
            company=self.company,
            project_name="BOM Test Quote",
            status=QuoteStatus.DRAFT,
        )
        QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=parent,
            quantity=Decimal("4"),
            unit_price=Decimal("100"),
        )

        # Accept the quote → triggers _deduct_inventory
        quote.status = QuoteStatus.ACCEPTED
        quote.save()

        # Parent: 50 - 4 = 46
        self.assertEqual(self._stock(parent), 46)
        # comp_a: 200 - (3×4) = 188
        self.assertEqual(self._stock(comp_a), 188)
        # comp_b: 100 - (1×4) = 96
        self.assertEqual(self._stock(comp_b), 96)

    def test_service_items_not_cascaded(self):
        """Service-type items should not trigger BOM deduction (or any deduction)."""
        from sales.models import Quote, QuoteInventoryItem, QuoteStatus

        service = self._make_item(
            "SRV-1", "Consulting",
            item_type=ItemType.SERVICE,
            default_warehouse=None,
        )

        quote = Quote.objects.create(
            company=self.company,
            project_name="Service Quote",
            status=QuoteStatus.DRAFT,
        )
        QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=service,
            quantity=Decimal("1"),
            unit_price=Decimal("500"),
        )

        tx_count_before = ItemTransaction.objects.count()

        quote.status = QuoteStatus.ACCEPTED
        quote.save()

        # No transactions should have been created for a service item
        self.assertEqual(ItemTransaction.objects.count(), tx_count_before)

    def test_nested_bom_on_quote_acceptance(self):
        """Nested BOMs are fully deducted when a quote is accepted."""
        from sales.models import Quote, QuoteInventoryItem, QuoteStatus

        raw = self._make_item("Q-RAW", stock=500)
        sub = self._make_item("Q-SUB", stock=100)
        top = self._make_item("Q-TOP", stock=30)

        BillOfMaterialsLine.objects.create(parent=sub, component=raw, quantity=10)
        BillOfMaterialsLine.objects.create(parent=top, component=sub, quantity=2)

        quote = Quote.objects.create(
            company=self.company,
            project_name="Nested BOM Quote",
            status=QuoteStatus.DRAFT,
        )
        QuoteInventoryItem.objects.create(
            quote=quote,
            inventory_item=top,
            quantity=Decimal("5"),
            unit_price=Decimal("200"),
        )

        quote.status = QuoteStatus.ACCEPTED
        quote.save()

        # top: 30 - 5 = 25
        self.assertEqual(self._stock(top), 25)
        # sub: 100 - (2×5) = 90
        self.assertEqual(self._stock(sub), 90)
        # raw: 500 - (10 × (2×5)) = 500 - 100 = 400
        self.assertEqual(self._stock(raw), 400)
