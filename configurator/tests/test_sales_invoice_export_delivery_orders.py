"""
Tests for the delivery orders exposed to sales invoice export templates: an
invoice template can print its albaranes (numbers, lines, shipping data)
without a second export.
"""
import json
from datetime import date
from decimal import Decimal

from django.test import TestCase

from company.models import Company
from configurator.services.export.sales_invoice import SalesInvoiceExportStrategy
from configurator.services.export.service import CustomJSONEncoder
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus
from sales.models import Client, DeliveryOrder, DeliveryOrderItem, ShippingAgency


class SalesInvoiceExportDeliveryOrderTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA,
        )
        SalesInvoiceItem.objects.create(
            invoice=self.invoice,
            description="Line",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
        )
        self.invoice.recalculate_totals()

    def _make_delivery_order(self, number, day, **kwargs):
        defaults = dict(
            company=self.company,
            client=self.client_obj,
            sales_invoice=self.invoice,
            number=number,
            date=date(2026, 9, day),
        )
        defaults.update(kwargs)
        return DeliveryOrder.objects.create(**defaults)

    def test_invoice_without_delivery_orders_reports_none(self):
        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        self.assertEqual(context["delivery_orders"], [])
        self.assertIsNone(context["delivery_order"])
        self.assertFalse(context["has_delivery_orders"])
        self.assertEqual(context["delivery_order_numbers"], [])

    def test_linked_delivery_orders_are_exposed_in_date_order(self):
        self._make_delivery_order("DO-2026-00002", 14)
        self._make_delivery_order("DO-2026-00001", 10)

        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        self.assertTrue(context["has_delivery_orders"])
        self.assertEqual(
            context["delivery_order_numbers"],
            ["DO-2026-00001", "DO-2026-00002"],
        )
        # The single-albarán shortcut points at the first one.
        self.assertEqual(context["delivery_order"]["number"], "DO-2026-00001")

    def test_delivery_order_carries_its_own_export_context(self):
        agency = ShippingAgency.objects.create(name="MRW", company=self.company)
        delivery_order = self._make_delivery_order(
            "DO-2026-00001",
            10,
            package_count=3,
            shipping_agency=agency,
            shipment_number="MRW-999",
        )
        DeliveryOrderItem.objects.create(
            delivery_order=delivery_order,
            description="Shipped door",
            quantity=Decimal("2"),
            unit_price=Decimal("50.00"),
            line_total=Decimal("100.00"),
        )

        do_context = SalesInvoiceExportStrategy(self.invoice).get_context()["delivery_orders"][0]

        self.assertEqual(do_context["date"], date(2026, 9, 10))
        self.assertEqual(do_context["package_count"], 3)
        self.assertEqual(do_context["shipping_agency"], agency)
        self.assertEqual(do_context["shipment_number"], "MRW-999")
        self.assertEqual(len(do_context["items"]), 1)
        self.assertEqual(do_context["items"][0]["description"], "Shipped door")
        self.assertEqual(do_context["items"][0]["quantity"], 2.0)
        self.assertEqual(do_context["line_tree"][0]["description"], "Shipped door")

    def test_company_block_is_not_repeated_per_delivery_order(self):
        self._make_delivery_order("DO-2026-00001", 10)

        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        self.assertEqual(context["company"], self.company)
        self.assertNotIn("company", context["delivery_orders"][0])
        self.assertNotIn("company_data", context["delivery_orders"][0])

    def test_context_with_delivery_orders_still_serializes(self):
        """Every export JSON-dumps the context for the template's data panel."""
        delivery_order = self._make_delivery_order("DO-2026-00001", 10)
        parent = DeliveryOrderItem.objects.create(
            delivery_order=delivery_order,
            description="Door",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            line_total=Decimal("100.00"),
        )
        DeliveryOrderItem.objects.create(
            delivery_order=delivery_order,
            description="Handle",
            quantity=Decimal("2"),
            parent_line=parent,
        )

        context = SalesInvoiceExportStrategy(self.invoice).get_context()
        payload = json.loads(json.dumps(context, cls=CustomJSONEncoder))

        do_payload = payload["delivery_orders"][0]
        self.assertEqual(do_payload["number"], "DO-2026-00001")
        self.assertEqual(
            [line["description"] for line in do_payload["items"]],
            ["Door", "Handle"],
        )
        self.assertEqual(do_payload["line_tree"][0]["components"][0]["description"], "Handle")

    def test_delivery_orders_of_other_invoices_are_excluded(self):
        other_invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            status=SalesInvoiceStatus.PROFORMA,
        )
        self._make_delivery_order("DO-2026-00001", 10)
        self._make_delivery_order("DO-2026-00002", 11, sales_invoice=other_invoice)

        context = SalesInvoiceExportStrategy(self.invoice).get_context()

        self.assertEqual(context["delivery_order_numbers"], ["DO-2026-00001"])
