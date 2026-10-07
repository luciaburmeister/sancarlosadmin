"""
inventory/tests/test_bulk_stock.py

Bulk stock-on-hand update from pasted spreadsheet rows: parsing, validation,
the all-or-nothing apply and the admin preview/confirm page.

Run:
    python manage.py test inventory.tests.test_bulk_stock
"""
from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import Client as HttpClient, TestCase
from django.urls import reverse
from django.utils import translation

from company.models import Company
from inventory.choices import ItemType, MovementType
from inventory.models import BillOfMaterialsLine, InventoryItem, ItemTransaction, Warehouse
from inventory.services.bulk_stock import apply_bulk_stock, build_plan, parse_rows


class _BulkStockTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.companies = Company.objects.filter(pk=cls.company.pk)
        cls.wh1 = Warehouse.objects.create(code="WH1", name="Main", company=cls.company)
        cls.wh2 = Warehouse.objects.create(code="WH2", name="Second", company=cls.company)
        cls.user = User.objects.create_superuser(
            username="admin", password="admin", email="admin@test.com"
        )

    def _make_item(self, code, stock=0, warehouse=None, **kwargs):
        item = InventoryItem.objects.create(
            code=code,
            name=code,
            company=self.company,
            item_type=kwargs.pop("item_type", ItemType.STOCKABLE),
            default_warehouse=kwargs.pop("default_warehouse", self.wh1),
            **kwargs,
        )
        if stock:
            ItemTransaction.objects.create(
                item=item,
                warehouse=warehouse or self.wh1,
                movement_type=MovementType.IN,
                quantity_change=stock,
            )
        return item

    def _balance(self, item, warehouse):
        return (
            ItemTransaction.objects.filter(item=item, warehouse=warehouse)
            .aggregate(total=Sum("quantity_change"))["total"] or 0
        )


class TestParseRows(_BulkStockTestCase):
    def test_tab_separated_paste_with_header(self):
        rows = parse_rows("Código\tCantidad\nA-1\t5\nA-2\t7\tWH2\n")
        self.assertEqual([(r.code, r.quantity, r.warehouse_code) for r in rows],
                         [("A-1", 5, ""), ("A-2", 7, "WH2")])

    def test_semicolon_with_spanish_number_formats(self):
        rows = parse_rows("A-1;1.500\nA-2;12,0\n")
        self.assertEqual([r.quantity for r in rows], [1500, 12])

    def test_comma_separated(self):
        rows = parse_rows("A-1,3\n\nA-2,4\n")
        self.assertEqual([r.quantity for r in rows], [3, 4])

    def test_fractional_and_negative_quantities_are_rejected(self):
        rows = parse_rows("A-1;1,5\nA-2;-3\n")
        self.assertTrue(all(r.errors for r in rows))


class TestBulkStockService(_BulkStockTestCase):
    def test_sets_counted_quantity_with_adjustments(self):
        a = self._make_item("A-1", stock=10)
        b = self._make_item("A-2", stock=3)
        c = self._make_item("A-3", stock=5)

        plan = apply_bulk_stock("A-1;4\nA-2;8\nA-3;5\n", self.companies, user=self.user)

        self.assertTrue(plan.is_valid)
        self.assertEqual(len(plan.changes), 2)
        self.assertEqual(self._balance(a, self.wh1), 4)
        self.assertEqual(self._balance(b, self.wh1), 8)
        self.assertEqual(self._balance(c, self.wh1), 5)
        adjust = ItemTransaction.objects.filter(movement_type=MovementType.ADJUST)
        self.assertEqual(sorted(adjust.values_list("quantity_change", flat=True)), [-6, 5])
        self.assertTrue(all(t.user == self.user for t in adjust))

    def test_warehouse_column_targets_that_warehouse_only(self):
        a = self._make_item("A-1", stock=10)
        self._make_item("A-1X")
        ItemTransaction.objects.create(
            item=a, warehouse=self.wh2, movement_type=MovementType.IN, quantity_change=2,
        )

        apply_bulk_stock("A-1;7;WH2\n", self.companies, user=self.user)

        self.assertEqual(self._balance(a, self.wh1), 10)
        self.assertEqual(self._balance(a, self.wh2), 7)

    def test_one_bad_row_applies_nothing(self):
        a = self._make_item("A-1", stock=10)

        plan = apply_bulk_stock("A-1;4\nMISSING;3\n", self.companies, user=self.user)

        self.assertFalse(plan.is_valid)
        self.assertEqual(self._balance(a, self.wh1), 10)
        self.assertFalse(ItemTransaction.objects.filter(movement_type=MovementType.ADJUST).exists())

    def test_rejects_duplicates_composites_services_and_unknown_warehouses(self):
        self._make_item("A-1")
        kit = self._make_item("KIT")
        part = self._make_item("PART")
        BillOfMaterialsLine.objects.create(parent=kit, component=part, quantity=1)
        self._make_item("SRV", item_type=ItemType.SERVICE, default_warehouse=None)

        plan = build_plan("A-1;1\nA-1;2\nKIT;1\nSRV;1\nPART;1;NOPE\n", self.companies)

        self.assertEqual([bool(r.errors) for r in plan.rows], [False, True, True, True, True])

    def test_items_of_other_companies_are_not_found(self):
        other = Company.objects.create(name="Other")
        other_wh = Warehouse.objects.create(code="WH1", name="Other", company=other)
        InventoryItem.objects.create(code="O-1", name="O", company=other, default_warehouse=other_wh)

        plan = build_plan("O-1;3\n", self.companies)

        self.assertFalse(plan.is_valid)


class TestBulkStockView(_BulkStockTestCase):
    def setUp(self):
        translation.activate("es")
        self.addCleanup(translation.deactivate)
        self.http = HttpClient()
        self.http.force_login(self.user)
        self.url = reverse("admin:inventory_inventoryitem_bulk_stock")

    def test_preview_writes_nothing_and_apply_writes(self):
        a = self._make_item("A-1", stock=10)

        r = self.http.post(self.url, {"rows": "A-1;4", "preview": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="apply"')
        self.assertEqual(self._balance(a, self.wh1), 10)

        r = self.http.post(self.url, {"rows": "A-1;4", "note": "Inventario anual", "apply": ""})
        self.assertRedirects(r, reverse("admin:inventory_inventoryitem_changelist"), fetch_redirect_response=False)
        self.assertEqual(self._balance(a, self.wh1), 4)
        self.assertEqual(
            ItemTransaction.objects.get(movement_type=MovementType.ADJUST).note, "Inventario anual",
        )

    def test_get_renders_form(self):
        r = self.http.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'name="rows"')

    def test_invalid_preview_has_no_apply_button(self):
        r = self.http.post(self.url, {"rows": "MISSING;4", "preview": ""})
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="apply"')
        self.assertTrue(r.context["form"].errors)

    def test_edit_returns_to_editable_textarea(self):
        self._make_item("A-1", stock=10)
        r = self.http.post(self.url, {"rows": "A-1;4", "edit": ""})
        self.assertNotContains(r, 'name="apply"')
        self.assertNotContains(r, "readonly")
