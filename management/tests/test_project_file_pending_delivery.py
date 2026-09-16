"""Items still owed to the client on an expediente
(management.services.project_file_status.get_pending_delivery_lines) and the
read-only block that renders them on the ProjectFile admin page."""
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from inventory.models import InventoryItem
from management.admin.project_file import ProjectFileAdmin
from management.models import ProjectFile
from management.services.project_file_status import get_pending_delivery_lines
from sales.models import (
    Client,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteInventoryItem,
    QuoteStatus,
)


class _PendingDeliveryBase(TestCase):

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF", company=self.company, client=self.client_obj,
        )
        self.config_ct = ContentType.objects.get_for_model(Configuration)
        self.inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

    def _quote(self, status=QuoteStatus.ACCEPTED):
        return Quote.objects.create(
            project_name="Quote", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=status,
        )

    def _config(self, quote, units="2", name="Config"):
        return Configuration.objects.create(
            name=name, company=self.company, product=self.product,
            client=self.client_obj, quote=quote,
            project_file=self.project_file if quote is None else None,
            status=ConfigurationStatus.COMPLETED, units=Decimal(units),
        )

    def _inventory_line(self, quote, quantity="5"):
        item = InventoryItem.objects.create(
            company=self.company, name="Hinge", code="HNG-1",
        )
        return QuoteInventoryItem.objects.create(
            quote=quote, inventory_item=item, quantity=Decimal(quantity),
        )

    def _delivery_order(self, status=DeliveryOrderStatus.SENT):
        return DeliveryOrder.objects.create(
            company=self.company, client=self.client_obj,
            project_file=self.project_file, status=status,
        )

    def _line(self, do, source, quantity, content_type=None):
        return DeliveryOrderItem.objects.create(
            delivery_order=do, description=str(source), quantity=Decimal(quantity),
            unit_price=Decimal("10.00"),
            source_content_type=content_type or self.config_ct,
            source_object_id=source.pk,
        )

    def _rows(self):
        return {row["reference"]: row for row in get_pending_delivery_lines(self.project_file)}


class PendingDeliveryLinesTests(_PendingDeliveryBase):

    def test_no_deliverables_gives_no_rows(self):
        self.assertEqual(get_pending_delivery_lines(self.project_file), [])

    def test_nothing_shipped_leaves_everything_pending(self):
        config = self._config(self._quote(), units="3")
        row = self._rows()[f"CFG-{config.pk}"]
        self.assertEqual(row["ordered"], Decimal("3"))
        self.assertEqual(row["shipped"], Decimal("0"))
        self.assertEqual(row["pending"], Decimal("3"))
        self.assertIn("Config", row["description"])

    def test_partial_shipment_reduces_pending(self):
        config = self._config(self._quote(), units="3")
        self._line(self._delivery_order(), config, "1")
        row = self._rows()[f"CFG-{config.pk}"]
        self.assertEqual(row["shipped"], Decimal("1"))
        self.assertEqual(row["pending"], Decimal("2"))

    def test_fully_shipped_line_has_zero_pending(self):
        config = self._config(self._quote(), units="2")
        self._line(self._delivery_order(), config, "2")
        self.assertEqual(self._rows()[f"CFG-{config.pk}"]["pending"], Decimal("0"))

    def test_over_delivery_never_goes_negative(self):
        config = self._config(self._quote(), units="1")
        self._line(self._delivery_order(), config, "3")
        self.assertEqual(self._rows()[f"CFG-{config.pk}"]["pending"], Decimal("0"))

    def test_draft_orders_are_reported_apart_and_still_pending(self):
        config = self._config(self._quote(), units="2")
        self._line(self._delivery_order(status=DeliveryOrderStatus.DRAFT), config, "2")
        row = self._rows()[f"CFG-{config.pk}"]
        self.assertEqual(row["shipped"], Decimal("0"))
        self.assertEqual(row["in_draft"], Decimal("2"))
        self.assertEqual(row["pending"], Decimal("2"))

    def test_cancelled_orders_do_not_count(self):
        config = self._config(self._quote(), units="2")
        self._line(self._delivery_order(status=DeliveryOrderStatus.CANCELLED), config, "2")
        row = self._rows()[f"CFG-{config.pk}"]
        self.assertEqual(row["shipped"], Decimal("0"))
        self.assertEqual(row["in_draft"], Decimal("0"))
        self.assertEqual(row["pending"], Decimal("2"))

    def test_quote_inventory_lines_are_included(self):
        quote = self._quote()
        inv_line = self._inventory_line(quote, quantity="5")
        self._line(self._delivery_order(), inv_line, "2", content_type=self.inv_item_ct)
        row = self._rows()["HNG-1"]
        self.assertEqual(row["ordered"], Decimal("5"))
        self.assertEqual(row["pending"], Decimal("3"))
        self.assertIn("Hinge", row["description"])

    def test_draft_quote_configurations_are_not_deliverable(self):
        self._config(self._quote(status=QuoteStatus.DRAFT), units="2")
        self.assertEqual(get_pending_delivery_lines(self.project_file), [])

    def test_direct_configuration_is_deliverable(self):
        config = self._config(None, units="1", name="Direct")
        row = self._rows()[f"CFG-{config.pk}"]
        self.assertEqual(row["pending"], Decimal("1"))
        self.assertEqual(row["description"], "Direct")


class PendingDeliveryAdminDisplayTests(_PendingDeliveryBase):

    def setUp(self):
        super().setUp()
        self.model_admin = ProjectFileAdmin(ProjectFile, AdminSite())

    def test_unsaved_object_renders_placeholder(self):
        self.assertEqual(self.model_admin.pending_delivery_display(ProjectFile()), "—")

    def test_lists_only_pending_lines(self):
        quote = self._quote()
        done = self._config(quote, units="1", name="Done")
        owed = self._config(quote, units="4", name="Owed")
        self._line(self._delivery_order(), done, "1")
        self._line(self._delivery_order(), owed, "1")

        html = self.model_admin.pending_delivery_display(self.project_file)
        self.assertIn("Owed", html)
        self.assertNotIn("Done", html)
        self.assertIn(f"CFG-{owed.pk}", html)
        self.assertIn("<td style=\"padding: 4px 0 4px 6px; text-align: right; font-weight: 600; color: #b45309;\">3</td>", html)

    def test_everything_delivered_message(self):
        config = self._config(self._quote(), units="1")
        self._line(self._delivery_order(), config, "1")
        html = self.model_admin.pending_delivery_display(self.project_file)
        self.assertIn("✓", html)
        self.assertNotIn("<table", html)

    def test_quantities_drop_trailing_zeros(self):
        config = self._config(self._quote(), units="2.500")
        html = self.model_admin.pending_delivery_display(self.project_file)
        self.assertIn(">2.5</td>", html)
        self.assertNotIn("2.500", html)
        self.assertEqual(config.units, Decimal("2.500"))
