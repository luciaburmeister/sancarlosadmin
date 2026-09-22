"""
A delivery order line that ships an item with a bill of materials gets one
component line per BOM component, nested as deep as the BOM goes. Component
lines carry no price of their own — the parent keeps that — but identity
codes can be linked to them.

Stock is the other way round: a composite item holds no goods of its own, so
shipping one moves its components and leaves the parent's own figure alone.

    python manage.py test sales.tests.test_delivery_order_components
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse

from company.models import Company
from configurator.services.export.delivery_order import DeliveryOrderExportStrategy
from inventory.choices import ItemType, MovementType
from inventory.models import (
    BillOfMaterialsLine,
    InventoryItem,
    ItemIdentityCode,
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


class _ComponentLinesBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.warehouse = Warehouse.objects.create(
            code="WH1", name="Main", company=cls.company,
        )
        cls.client_obj = Client.objects.create(name="Acme", company=cls.company)
        cls.project_file = ProjectFile.objects.create(
            name="PF", company=cls.company, client=cls.client_obj,
        )
        cls.quote_line_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

    def _item(self, code, stock=0, **kwargs):
        item = InventoryItem.objects.create(
            code=code, name=kwargs.pop("name", f"Item {code}"), company=self.company,
            item_type=kwargs.pop("item_type", ItemType.STOCKABLE),
            default_warehouse=kwargs.pop("default_warehouse", self.warehouse),
            **kwargs,
        )
        if stock:
            ItemTransaction.objects.create(
                item=item, warehouse=self.warehouse,
                movement_type=MovementType.IN, quantity_change=stock,
                note="Initial stock",
            )
        return item

    def _stock(self, item):
        return item.stock_on_hand

    def _kit(self):
        """Window (top) = 2 × Frame (sub) + 1 × Handle; Frame = 4 × Profile."""
        window = self._item("WIN", stock=10, name="Window")
        frame = self._item("FRM", stock=100, name="Frame")
        handle = self._item("HND", stock=50, name="Handle")
        profile = self._item("PRF", stock=1000, name="Profile")
        BillOfMaterialsLine.objects.create(parent=window, component=frame, quantity=2)
        BillOfMaterialsLine.objects.create(parent=window, component=handle, quantity=1)
        BillOfMaterialsLine.objects.create(parent=frame, component=profile, quantity=4)
        # has_components is written by a signal, so the instances held here
        # still carry the value they had before the lines existed.
        for item in (window, frame, handle, profile):
            item.refresh_from_db()
        return window, frame, handle, profile

    def _accepted_quote_line(self, item, quantity):
        quote = Quote.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, project_name="Q",
            status=QuoteStatus.ACCEPTED,
        )
        return QuoteInventoryItem.objects.create(
            quote=quote, inventory_item=item,
            quantity=Decimal(quantity), unit_price=Decimal("100"),
        )

    def _deliver(self, quote_line, quantity=None):
        return DeliveryOrderService.create_delivery_order(
            project_file=self.project_file, company=self.company,
            client=self.client_obj,
            items_data=[{
                "source_content_type_id": self.quote_line_ct.id,
                "source_object_id": quote_line.pk,
                "description": quote_line.inventory_item.name,
                "quantity": quantity if quantity is not None else quote_line.quantity,
                "unit_price": quote_line.unit_price.amount,
            }],
        )

    def _parent_line(self, do):
        return do.items.top_level().get()

    def _component(self, parent_line, item):
        return parent_line.component_lines.get(inventory_item=item)


class ExplosionTests(_ComponentLinesBase):

    def test_bom_becomes_component_lines_under_the_parent(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        parent = self._parent_line(do)
        self.assertEqual(parent.inventory_item, window)
        self.assertFalse(parent.is_component)

        frame_line = self._component(parent, frame)
        handle_line = self._component(parent, handle)
        self.assertEqual(frame_line.quantity, Decimal("6"))
        self.assertEqual(handle_line.quantity, Decimal("3"))
        for line in (frame_line, handle_line):
            self.assertTrue(line.is_component)
            self.assertEqual(line.delivery_order, do)
            self.assertEqual(line.unit_price.amount, Decimal("0"))
            self.assertEqual(line.line_total.amount, Decimal("0"))
            self.assertEqual(line.reference, line.inventory_item.code)
            self.assertEqual(line.description, line.inventory_item.name)
            self.assertTrue(line.item_number.startswith("DOI-"))

    def test_nested_bom_becomes_nested_lines(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        frame_line = self._component(self._parent_line(do), frame)
        profile_line = self._component(frame_line, profile)
        self.assertEqual(profile_line.quantity, Decimal("24"))  # 4 × (2 × 3)
        self.assertEqual(profile_line.depth, 2)
        self.assertEqual(do.items.count(), 4)
        self.assertEqual(do.items.components().count(), 3)

    def test_circular_bom_stops_at_the_repeated_item(self):
        a = self._item("CYC-A", stock=10)
        b = self._item("CYC-B", stock=10)
        BillOfMaterialsLine.objects.create(parent=a, component=b, quantity=1)
        BillOfMaterialsLine.objects.create(parent=b, component=a, quantity=1)

        do = self._deliver(self._accepted_quote_line(a, "1"))

        self.assertEqual(do.items.count(), 2)
        b_line = self._component(self._parent_line(do), b)
        self.assertFalse(b_line.component_lines.exists())

    def test_item_without_bom_gets_no_component_lines(self):
        plain = self._item("PLAIN", stock=5)
        do = self._deliver(self._accepted_quote_line(plain, "2"))
        self.assertEqual(do.items.count(), 1)

    def test_configuration_lines_get_no_component_lines(self):
        from configurator.models import Configuration, ConfigurationStatus, ProductTemplate

        product = ProductTemplate.objects.create(name="Door", company=self.company)
        config = Configuration.objects.create(
            name="Config", company=self.company, product=product,
            client=self.client_obj, project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=Decimal("100"), total_price=Decimal("121"),
        )
        do = DeliveryOrderService.create_delivery_order(
            project_file=self.project_file, company=self.company,
            client=self.client_obj,
            items_data=[{
                "source_content_type_id": ContentType.objects.get_for_model(Configuration).id,
                "source_object_id": config.pk,
                "description": config.name,
                "quantity": Decimal("1"),
                "unit_price": Decimal("100"),
            }],
        )
        line = do.items.get()
        self.assertIsNone(line.inventory_item)
        self.assertEqual(do.items.count(), 1)


class StockAndTotalsTests(_ComponentLinesBase):

    def test_stock_leaves_through_the_components_not_the_composite(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        # Window and Frame are recipes; only the goods on a shelf move.
        self.assertEqual(self._stock(window), 10)
        self.assertEqual(self._stock(frame), 100)
        self.assertEqual(self._stock(handle), 47)      # 50 - 1×3
        self.assertEqual(self._stock(profile), 976)    # 1000 - 4×2×3

        self.assertTrue(self._parent_line(do).stock_deducted)
        for line in do.items.components():
            self.assertFalse(line.stock_deducted)

    def test_an_item_without_a_bom_still_moves_its_own_stock(self):
        plain = self._item("PLAIN", stock=5)
        self._deliver(self._accepted_quote_line(plain, "2"))
        self.assertEqual(self._stock(plain), 3)

    def test_a_composite_of_a_non_stockable_type_still_moves_its_components(self):
        kit = self._item("KIT", stock=10, item_type=ItemType.FINISHED_PRODUCT)
        part = self._item("PART", stock=40)
        BillOfMaterialsLine.objects.create(parent=kit, component=part, quantity=3)

        self._deliver(self._accepted_quote_line(kit, "2"))

        self.assertEqual(self._stock(kit), 10)
        self.assertEqual(self._stock(part), 34)        # 40 - 3×2

    def test_component_lines_do_not_change_totals(self):
        window, *_ = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))
        self.assertEqual(do.subtotal_amount.amount, Decimal("300.00"))
        self.assertEqual(do.total_amount.amount, Decimal("300.00"))

    def test_cancelling_returns_stock_exactly_once(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        do.status = DeliveryOrderStatus.CANCELLED
        do.save()

        self.assertEqual(self._stock(window), 10)
        self.assertEqual(self._stock(frame), 100)
        self.assertEqual(self._stock(handle), 50)
        self.assertEqual(self._stock(profile), 1000)

    def test_deleting_the_parent_line_removes_components_and_returns_stock_once(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        self._parent_line(do).delete()

        self.assertEqual(do.items.count(), 0)
        self.assertEqual(self._stock(window), 10)
        self.assertEqual(self._stock(frame), 100)
        self.assertEqual(self._stock(profile), 1000)

    def test_deleting_the_order_returns_stock_once(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        do.delete()

        self.assertEqual(self._stock(window), 10)
        self.assertEqual(self._stock(frame), 100)
        self.assertEqual(self._stock(profile), 1000)

    def test_changing_the_parent_quantity_resizes_the_components(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))
        parent = self._parent_line(do)
        frame_line = self._component(parent, frame)
        profile_line = self._component(frame_line, profile)

        parent.quantity = Decimal("5")
        parent.save()

        frame_line.refresh_from_db()
        profile_line.refresh_from_db()
        self.assertEqual(frame_line.quantity, Decimal("10"))
        self.assertEqual(profile_line.quantity, Decimal("40"))
        # Re-sized in place, not recreated.
        self.assertEqual(do.items.components().count(), 3)

    def test_resizing_keeps_codes_linked_to_components(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))
        parent = self._parent_line(do)
        frame_line = self._component(parent, frame)
        code = ItemIdentityCode.objects.create(item=frame, code="FRM-001", delivery_order_item=frame_line)

        parent.quantity = Decimal("4")
        parent.save()

        code.refresh_from_db()
        self.assertEqual(code.delivery_order_item_id, frame_line.pk)

    def test_invoice_from_delivery_order_skips_component_lines(self):
        from django.utils import timezone

        window, *_ = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "3"))

        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            delivery_orders=[do], company=self.company, client=self.client_obj,
            date_issued=timezone.now().date(),
        )

        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("300.00"))


class ManualProductLineTests(_ComponentLinesBase):

    def _draft_order(self):
        return DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=DeliveryOrderStatus.DRAFT,
        )

    def test_picking_an_item_fills_the_line_and_moves_stock(self):
        window, frame, handle, profile = self._kit()
        window.selling_price = Decimal("250.00")
        window.save()
        do = self._draft_order()

        line = DeliveryOrderItem.objects.create(
            delivery_order=do, inventory_item=window, quantity=Decimal("2"),
        )

        self.assertEqual(line.description, "Window")
        self.assertEqual(line.reference, "WIN")
        self.assertEqual(line.unit_price.amount, Decimal("250.00"))
        self.assertEqual(line.line_total.amount, Decimal("500.00"))
        self.assertTrue(line.stock_deducted)
        self.assertEqual(self._stock(window), 10)      # composite
        self.assertEqual(self._stock(frame), 100)      # composite
        self.assertEqual(self._stock(profile), 984)    # 1000 - 4×2×2
        self.assertEqual(self._component(line, frame).quantity, Decimal("4"))

    def test_an_explicit_price_is_kept(self):
        window, *_ = self._kit()
        window.selling_price = Decimal("250.00")
        window.save()
        line = DeliveryOrderItem.objects.create(
            delivery_order=self._draft_order(), inventory_item=window,
            quantity=Decimal("1"), unit_price=Decimal("180.00"),
            description="Special window",
        )
        self.assertEqual(line.unit_price.amount, Decimal("180.00"))
        self.assertEqual(line.description, "Special window")

    def test_quote_sourced_line_resolves_its_inventory_item(self):
        plain = self._item("PLAIN", stock=5)
        do = self._deliver(self._accepted_quote_line(plain, "2"))
        line = do.items.get()
        self.assertEqual(line.inventory_item, plain)
        self.assertEqual(DeliveryOrderItem.objects.get(pk=line.pk).inventory_item_id, plain.pk)


class LinkCodesPageTests(_ComponentLinesBase):

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw",
        )
        self.client.force_login(self.superuser)

    def _url(self, do):
        return reverse("admin:sales_deliveryorder_link_identity_codes", args=[do.pk])

    def test_component_lines_are_offered_indented_under_their_parent(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))

        response = self.client.get(self._url(do))
        self.assertEqual(response.status_code, 200)

        entries = response.context["line_items"]
        self.assertEqual(
            [(e["obj"].inventory_item.code, e["depth"]) for e in entries],
            [("WIN", 0), ("FRM", 1), ("PRF", 2), ("HND", 1)],
        )

    def test_a_code_can_be_linked_to_a_component_line(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))
        frame_line = self._component(self._parent_line(do), frame)
        code = ItemIdentityCode.objects.create(item=frame, code="FRM-001")

        response = self.client.post(
            self._url(do), {"target_item": frame_line.pk, "codes": [code.pk]},
        )
        self.assertEqual(response.status_code, 302)

        code.refresh_from_db()
        self.assertEqual(code.delivery_order_item, frame_line)
        self.assertIsNotNone(code.delivered_at)

    def test_component_capacity_is_its_own_quantity(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))
        handle_line = self._component(self._parent_line(do), handle)  # qty 1
        codes = [
            ItemIdentityCode.objects.create(item=handle, code=f"HND-{i}") for i in range(2)
        ]

        self.client.post(
            self._url(do), {"target_item": handle_line.pk, "codes": [c.pk for c in codes]},
        )

        self.assertEqual(handle_line.identity_codes.count(), 0)

    def test_a_code_of_another_item_cannot_be_linked_to_a_component(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))
        frame_line = self._component(self._parent_line(do), frame)
        wrong = ItemIdentityCode.objects.create(item=handle, code="HND-X")

        self.client.post(self._url(do), {"target_item": frame_line.pk, "codes": [wrong.pk]})

        wrong.refresh_from_db()
        self.assertIsNone(wrong.delivery_order_item)


class AdminAndExportTests(_ComponentLinesBase):

    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="admin", email="admin@example.com", password="pw",
        )
        self.client.force_login(self.superuser)

    def test_change_form_lists_components_read_only_and_apart_from_items(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))

        response = self.client.get(reverse("admin:sales_deliveryorder_change", args=[do.pk]))
        self.assertEqual(response.status_code, 200)

        by_prefix = {
            inline.formset.prefix: inline
            for inline in response.context["inline_admin_formsets"]
            if inline.formset.model is DeliveryOrderItem
        }
        self.assertEqual(len(by_prefix), 2, by_prefix.keys())
        items, components = sorted(by_prefix.values(), key=lambda i: len(i.formset.prefix))
        self.assertEqual([f.instance.inventory_item.code for f in items.formset.forms], ["WIN"])
        self.assertEqual(
            sorted(f.instance.inventory_item.code for f in components.formset.forms),
            ["FRM", "HND", "PRF"],
        )
        self.assertFalse(components.has_change_permission)
        self.assertFalse(components.has_delete_permission)

    def test_export_context_nests_components_under_their_parent(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))
        frame_line = self._component(self._parent_line(do), frame)
        ItemIdentityCode.objects.create(item=frame, code="FRM-001", delivery_order_item=frame_line)

        items = DeliveryOrderExportStrategy(do).get_context()["items"]

        self.assertEqual(
            [(i["reference"], i["depth"], i["is_component"]) for i in items],
            [("WIN", 0, False), ("FRM", 1, True), ("PRF", 2, True), ("HND", 1, True)],
        )
        parent_number = self._parent_line(do).item_number
        self.assertEqual(items[1]["parent_item_number"], parent_number)
        self.assertEqual(items[1]["parent_description"], "Window")
        self.assertEqual(items[1]["parent_reference"], "WIN")
        self.assertEqual(items[1]["identity_codes"], ["FRM-001"])
        self.assertEqual(items[1]["line_total"], 0)

    def test_export_context_exposes_the_component_tree(self):
        window, frame, handle, profile = self._kit()
        do = self._deliver(self._accepted_quote_line(window, "1"))

        context = DeliveryOrderExportStrategy(do).get_context()
        tree = context["line_tree"]

        self.assertTrue(context["has_components"])
        self.assertEqual([i["reference"] for i in tree], ["WIN"])
        window_line = tree[0]
        self.assertEqual([c["reference"] for c in window_line["components"]], ["FRM", "HND"])
        # Every descendant, flattened, for a parent row followed by its BOM.
        self.assertEqual(
            [c["reference"] for c in window_line["all_components"]], ["FRM", "PRF", "HND"],
        )
        self.assertEqual(window_line["component_count"], 3)
        self.assertTrue(window_line["has_components"])

        frame_data, handle_data = window_line["components"]
        self.assertEqual([c["reference"] for c in frame_data["components"]], ["PRF"])
        self.assertFalse(handle_data["has_components"])
        self.assertFalse(handle_data["components"])
        self.assertEqual([frame_data["is_last_child"], handle_data["is_last_child"]], [False, True])

        # A component at any depth points back at the priced line it belongs to.
        profile_data = frame_data["components"][0]
        self.assertEqual(profile_data["parent_reference"], "FRM")
        self.assertEqual(profile_data["root_item_number"], window_line["item_number"])
        self.assertEqual(profile_data["root_description"], "Window")
        self.assertEqual(profile_data["indent_px"], 28)

    def test_export_context_of_a_delivery_order_without_components(self):
        plain = self._item("PLN", stock=5, name="Plain")
        do = self._deliver(self._accepted_quote_line(plain, "1"))

        context = DeliveryOrderExportStrategy(do).get_context()

        self.assertFalse(context["has_components"])
        self.assertEqual(len(context["line_tree"]), 1)
        self.assertEqual(context["line_tree"][0]["components"], [])
        self.assertEqual(context["line_tree"][0]["root_item_number"], context["items"][0]["item_number"])
