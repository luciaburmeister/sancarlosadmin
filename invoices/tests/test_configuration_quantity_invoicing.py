"""A configuration of 3 pieces at 100.00 (IVA 21 %) bills 300.00 + 63.00 =
363.00 on every invoicing path: from the expediente (component lines or one
line per configuration), through delivery orders (whole or partial) and the
direct configuration pro forma."""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, TaxType
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService
from management.models import ProjectFile
from sales.models import Client, Quote, QuoteStatus


class ConfigurationQuantityInvoicingTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Piezas SL")
        self.client_obj = Client.objects.create(name="Cliente SA", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="Obra", company=self.company, client=self.client_obj,
        )
        self.quote = Quote.objects.create(
            project_name="Obra", company=self.company, client=self.client_obj,
            project_file=self.project_file, status=QuoteStatus.ACCEPTED,
        )
        self.config = Configuration.objects.create(
            name="Ventana",
            company=self.company,
            product=ProductTemplate.objects.create(name="Ventana", company=self.company),
            client=self.client_obj,
            quote=self.quote,
            status=ConfigurationStatus.DRAFT,
            price_override=Money(Decimal("100.00"), "EUR"),
            quantity=3,
        )
        tax_type, _created = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"},
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=self.config.pk,
            tax_type=tax_type,
            selected_percentage=Decimal("21"),
            order=0,
        )
        self.config.recalculate_price()
        self.config.refresh_from_db()

    def _invoice_from_project_file(self, **kwargs):
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 10, 1), due_date=None,
            items_data=ProjectFileInvoiceService.build_items_for_ratio(summary, Decimal("1"), **kwargs),
        )

    def _deliverable(self):
        (entry,) = DeliveryOrderService.get_deliverable_items(self.project_file)
        return entry

    def _deliver(self, quantity):
        entry = self._deliverable()
        return DeliveryOrderService.create_delivery_order(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            items_data=[{
                "source_content_type_id": entry["source_content_type_id"],
                "source_object_id": entry["source_object_id"],
                "description": entry["description"],
                "quantity": quantity,
                "unit_price": entry["unit_price"],
                "discount_percentage": Decimal("0.00"),
            }],
        )

    def _invoice_delivery_orders(self, delivery_orders):
        return DeliveryOrderService.create_invoice_from_delivery_orders(
            delivery_orders, self.company, self.client_obj, date(2026, 10, 1),
        )

    def test_invoice_from_project_file_bills_every_piece(self):
        invoice = self._invoice_from_project_file()

        self.assertEqual(invoice.subtotal_amount.amount, Decimal("300.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("363.00"))

    def test_invoice_with_one_line_per_configuration_bills_every_piece(self):
        invoice = self._invoice_from_project_file(split_configs=False)

        self.assertEqual(invoice.total_amount.amount, Decimal("363.00"))

    def test_delivery_order_offers_one_unit_per_piece(self):
        entry = self._deliverable()

        self.assertEqual(entry["total_quantity"], Decimal("3"))
        self.assertEqual(entry["unit_price"], Decimal("100.00"))

    def test_invoicing_the_delivery_order_bills_every_piece(self):
        delivery_order = self._deliver(Decimal("3"))

        invoice = self._invoice_delivery_orders([delivery_order])

        self.assertEqual(invoice.total_amount.amount, Decimal("363.00"))

    def test_partial_delivery_leaves_the_remaining_pieces(self):
        delivery_order = self._deliver(Decimal("1"))

        remaining = self._deliverable()
        invoice = self._invoice_delivery_orders([delivery_order])

        self.assertEqual(remaining["remaining_quantity"], Decimal("2"))
        self.assertEqual(remaining["unit_price"], Decimal("100.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))

    def test_direct_pro_forma_bills_every_piece(self):
        invoice = SalesInvoiceService.create_from_configuration(self.config)
        invoice.refresh_from_db()

        self.assertEqual(invoice.total_amount.amount, Decimal("363.00"))
