"""What-to-order report: pending to serve, incoming, and the shortfall to buy.

Run:
    python manage.py test inventory.tests.test_reorder_report
"""
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.utils.translation import gettext as _

from inventory.choices import ItemType
from inventory.models import BillOfMaterialsLine, InventoryItem
from inventory.services.reorder_report import reorder_report_rows, reorder_report_xlsx
from inventory.tests.test_stock_position import _StockPositionTestCase
from purchasing.models import PurchaseOrderStatus
from sales.models import QuoteStatus


class ReorderReportTests(_StockPositionTestCase):

    def _rows(self):
        return reorder_report_rows(InventoryItem.objects.filter(company=self.company))

    def test_recommends_what_stock_and_incoming_do_not_cover(self):
        item = self._make_item("A", stock=4)
        self._accepted_quote_line(item, 10)
        self._order(item, 3)

        rows = self._rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "A")
        self.assertEqual(rows[0].stock, Decimal("4"))
        self.assertEqual(rows[0].pending_to_serve, Decimal("10"))
        self.assertEqual(rows[0].incoming, Decimal("3"))
        self.assertEqual(rows[0].recommended, Decimal("3"))

    def test_covered_item_stays_listed_with_nothing_to_order(self):
        item = self._make_item("B", stock=10)
        self._accepted_quote_line(item, 4)

        rows = self._rows()

        self.assertEqual(rows[0].pending_to_serve, Decimal("4"))
        self.assertEqual(rows[0].recommended, Decimal("0"))

    def test_delivered_quantity_is_netted_off(self):
        item = self._make_item("C", stock=10)
        line = self._accepted_quote_line(item, 8)
        self._deliver(line, quantity=Decimal("5"))

        rows = self._rows()

        self.assertEqual(rows[0].stock, Decimal("5"))
        self.assertEqual(rows[0].pending_to_serve, Decimal("3"))
        self.assertEqual(rows[0].recommended, Decimal("0"))

    def test_nothing_pending_is_omitted(self):
        self._make_item("D", stock=5)
        self._accepted_quote_line(self._make_item("E"), 2, status=QuoteStatus.DRAFT)

        self.assertEqual(self._rows(), [])

    def test_unapproved_purchase_does_not_reduce_the_recommendation(self):
        item = self._make_item("F", stock=0)
        self._accepted_quote_line(item, 5)
        self._order(item, 5, status=PurchaseOrderStatus.PENDING_APPROVAL)

        rows = self._rows()

        self.assertEqual(rows[0].incoming, Decimal("0"))
        self.assertEqual(rows[0].recommended, Decimal("5"))

    def test_service_items_are_omitted(self):
        service = InventoryItem.objects.create(
            code="SRV",
            name="Install",
            company=self.company,
            item_type=ItemType.SERVICE,
        )
        self._accepted_quote_line(service, 2)

        self.assertEqual(self._rows(), [])

    def test_a_kit_component_is_not_listed_on_its_own(self):
        kit = self._make_item("KIT")
        part = self._make_item("PART")
        BillOfMaterialsLine.objects.create(parent=kit, component=part, quantity=2)
        self._accepted_quote_line(kit, 3)

        rows = self._rows()

        self.assertEqual([row.code for row in rows], ["KIT"])
        self.assertEqual(rows[0].pending_to_serve, Decimal("3"))

    def test_xlsx_lists_the_same_figures(self):
        item = self._make_item("G", stock=1)
        item.name = "Manguito"
        item.save(update_fields=["name"])
        self._accepted_quote_line(item, 6)

        book = openpyxl.load_workbook(BytesIO(reorder_report_xlsx(self._rows())))
        sheet = book.active

        self.assertEqual(
            [cell.value for cell in sheet[1]],
            [
                _("Code"),
                _("Article"),
                _("Stock"),
                _("Pending to serve"),
                _("Pending to receive (on the way)"),
                _("Recommended to order"),
            ],
        )
        self.assertEqual(sheet["A2"].value, "G")
        self.assertEqual(sheet["B2"].value, "Manguito")
        self.assertEqual(sheet["C2"].value, 1)
        self.assertEqual(Decimal(str(sheet["D2"].value)), Decimal("6"))
        self.assertEqual(Decimal(str(sheet["E2"].value)), Decimal("0"))
        self.assertEqual(Decimal(str(sheet["F2"].value)), Decimal("5"))
