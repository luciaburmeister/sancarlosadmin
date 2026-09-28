"""
inventory/tests/test_resource_stock_position.py

The cached stock-position columns are export-only in InventoryItemResource:
they show up in a download, and an upload carrying them cannot write them.

Run:
    python manage.py test inventory.tests.test_resource_stock_position
"""
from decimal import Decimal

import tablib
from django.test import TestCase

from company.models import Company
from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction, Warehouse
from inventory.resources import InventoryItemResource
from materials.models import Provider
from purchasing.models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    PurchaseOrderType,
)
from sales.models import Client, Quote, QuoteInventoryItem, QuoteStatus


class InventoryItemResourceStockPositionTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.warehouse = Warehouse.objects.create(
            code="WH1", name="Main", company=cls.company,
        )
        cls.provider = Provider.objects.create(company=cls.company, name="Proveedor SA")
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)

    def setUp(self):
        self.item = InventoryItem.objects.create(
            code="EXP-1", name="Widget", company=self.company,
            item_type=ItemType.STOCKABLE, default_warehouse=self.warehouse,
        )
        po = PurchaseOrder.objects.create(
            company=self.company, provider=self.provider,
            po_type=PurchaseOrderType.STOCK, status=PurchaseOrderStatus.APPROVED,
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po, inventory_item=self.item,
            quantity=Decimal("7"), unit_price=Decimal("10"),
        )
        quote = Quote.objects.create(
            company=self.company, client=self.client_obj, status=QuoteStatus.ACCEPTED,
        )
        QuoteInventoryItem.objects.create(
            quote=quote, inventory_item=self.item,
            quantity=Decimal("3"), unit_price=Decimal("50"),
        )
        ItemTransaction.objects.create(
            item=self.item, warehouse=self.warehouse,
            movement_type=MovementType.IN, quantity_change=12, note="Initial stock",
        )
        self.item.refresh_from_db()

    # Column headings are translated and the suite runs under the project's
    # own LANGUAGE_CODE, so they are looked up from the resource rather than
    # spelled out.
    @staticmethod
    def _heading(field_name):
        return str(InventoryItemResource().fields[field_name].column_name)

    def _export_row(self, **kwargs):
        data = InventoryItemResource().export(
            InventoryItem.objects.filter(pk=self.item.pk), **kwargs
        )
        return data, dict(zip(data.headers, data[0]))

    def test_export_includes_the_stock_position_columns(self):
        incoming, reserved = self._heading("incoming_quantity"), self._heading("reserved_quantity")
        # force_native_type is what the admin passes for xlsx: the cell must be
        # a real number, not the string "7.000", so it sorts and sums.
        data, row = self._export_row(force_native_type=True)
        self.assertIn(incoming, data.headers)
        self.assertIn(reserved, data.headers)
        self.assertIsInstance(row[incoming], Decimal)
        self.assertEqual(row[incoming], Decimal("7"))
        self.assertEqual(row[reserved], Decimal("3"))

    def test_export_includes_stock_on_hand(self):
        heading = self._heading("stock_on_hand")
        data, row = self._export_row(force_native_type=True)
        self.assertIn(heading, data.headers)
        self.assertEqual(row[heading], 12)

    def test_export_reads_the_stock_on_hand_annotation(self):
        """The admin exports its annotated changelist queryset; the column
        must use that rather than aggregate once per row."""
        qs = (
            InventoryItem.objects.filter(pk=self.item.pk)
            .select_related("company", "default_warehouse", "preferred_supplier")
            .with_stock_on_hand()
        )
        items = list(qs)
        with self.assertNumQueries(0):
            data = InventoryItemResource().export(items)
        self.assertEqual(dict(zip(data.headers, data[0]))[self._heading("stock_on_hand")], "12")

    def test_export_places_them_after_min_stock(self):
        data, _ = self._export_row()
        self.assertEqual(
            data.headers[-2:],
            [self._heading("incoming_quantity"), self._heading("reserved_quantity")],
        )

    def test_import_ignores_the_stock_position_columns(self):
        """The whole point of readonly: a round-tripped spreadsheet with these
        columns edited must not be able to set them by hand."""
        dataset = tablib.Dataset(
            headers=[
                "id", "code", "name",
                self._heading("incoming_quantity"), self._heading("reserved_quantity"),
                self._heading("stock_on_hand"),
            ]
        )
        dataset.append([self.item.pk, "EXP-1", "Widget Renamed", "999", "888", "777"])

        result = InventoryItemResource().import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())

        self.item.refresh_from_db()
        # The editable column took the change...
        self.assertEqual(self.item.name, "Widget Renamed")
        # ...while the derived ones kept their computed values.
        self.assertEqual(self.item.incoming_quantity, Decimal("7"))
        self.assertEqual(self.item.reserved_quantity, Decimal("3"))
        self.assertEqual(self.item.stock_on_hand, 12)

    def test_import_without_the_columns_still_works(self):
        dataset = tablib.Dataset(headers=["id", "code", "name"])
        dataset.append([self.item.pk, "EXP-1", "Renamed Again"])

        result = InventoryItemResource().import_data(dataset, raise_errors=True)
        self.assertFalse(result.has_errors())

        self.item.refresh_from_db()
        self.assertEqual(self.item.name, "Renamed Again")
        self.assertEqual(self.item.incoming_quantity, Decimal("7"))
        self.assertEqual(self.item.reserved_quantity, Decimal("3"))
