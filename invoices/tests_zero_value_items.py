"""
Tests for zero-value items being kept as 0-amount invoice lines.

Zero-value sources (configurations, quote items, inventory items, direct
configurations) must appear on the invoice as 0 € lines in all three
item-building flows: the admin wizard, the mobile/API builder and the
bulk invoice service. Only negative lines are dropped, and a 0 invoicing
ratio still yields no lines at all.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from inventory.models import InventoryItem
from invoices.models import AppliedTax, TaxType
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from materials.models import Category, Material
from sales.models import Client, Quote, QuoteInventoryItem, QuoteItem, QuoteStatus


class _ZeroValueItemsTestBase(TestCase):
    """One accepted quote with a 100 € config plus zero-value sources of
    every kind, and a zero-value direct configuration."""

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(name="Door", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF Zero", company=self.company, client=self.client_obj,
        )
        self.iva21, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )

        self.quote = Quote.objects.create(
            project_name="Q Zero",
            company=self.company,
            client=self.client_obj,
            project_file=self.project_file,
            status=QuoteStatus.ACCEPTED,
            subtotal_price=Decimal("100.00"),
            tax_amount=Decimal("21.00"),
            total_price=Decimal("121.00"),
        )

        self.config_normal = self._create_config("Puerta", Decimal("100.00"))
        self.config_zero = self._create_config("Puerta gratis", Decimal("0.00"))
        for config in (self.config_normal, self.config_zero):
            AppliedTax.objects.create(
                content_type=ContentType.objects.get_for_model(Configuration),
                object_id=config.pk,
                tax_type=self.iva21,
                selected_percentage=Decimal("21"),
                base_amount=config.subtotal_price.amount,
                calculated_amount=(
                    config.subtotal_price.amount * Decimal("0.21")
                ).quantize(Decimal("0.01")),
            )

        category = Category.objects.create(name="Perfiles", company=self.company)
        material = Material.objects.create(
            reference_code="MAT-0", name="Perfil", category=category,
            cost_per_unit=Decimal("0"),
        )
        self.quote_item_zero = QuoteItem.objects.create(
            quote=self.quote,
            material=material,
            quantity=Decimal("1"),
            unit_price=Decimal("0.00"),
        )

        stock_item = InventoryItem.objects.create(name="Regalo", company=self.company)
        self.inv_item_zero = QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=stock_item,
            quantity=Decimal("2"),
            unit_price=Decimal("0.00"),
            line_total=Decimal("0.00"),
            total_with_tax=Decimal("0.00"),
        )

        # Direct (non-quote) configuration worth 0 €
        self.direct_zero = Configuration.objects.create(
            name="Ajuste gratis",
            company=self.company,
            product=self.product,
            client=self.client_obj,
            project_file=self.project_file,
            status=ConfigurationStatus.SHOP,
            subtotal_price=Decimal("0.00"),
            total_price=Decimal("0.00"),
        )

        # Child saves trigger quote recalcs — pin the intended amounts.
        Quote.objects.filter(pk=self.quote.pk).update(
            subtotal_price=Decimal("100.00"),
            discounted_subtotal=Decimal("100.00"),
            tax_amount=Decimal("21.00"),
            total_price=Decimal("121.00"),
        )
        self.quote.refresh_from_db()
        self.summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)

    def _create_config(self, name, subtotal):
        return Configuration.objects.create(
            name=name,
            company=self.company,
            product=self.product,
            client=self.client_obj,
            quote=self.quote,
            project_file=self.project_file,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=subtotal,
            total_price=(subtotal * Decimal("1.21")).quantize(Decimal("0.01")),
        )

    def _source_ids(self, items):
        return {i["source_id"] for i in items}

    def _expected_source_ids(self):
        return {
            f"config_{self.config_normal.pk}",
            f"config_{self.config_zero.pk}",
            f"quote_item_{self.quote_item_zero.pk}",
            f"inv_item_{self.inv_item_zero.pk}",
            f"direct_{self.direct_zero.pk}",
        }


class WizardZeroValueItemsTests(_ZeroValueItemsTestBase):
    """_build_items_initial in the project-file invoice wizard."""

    def _build_items(self, calculated_subtotal=None):
        from invoices.views.invoice.project_file_wizard_views import (
            ProjectFileInvoiceWizardView,
        )
        if calculated_subtotal is None:
            calculated_subtotal = self.summary["remaining_subtotal"]
        view = ProjectFileInvoiceWizardView()
        view.invoicing_summary = self.summary
        view.get_cleaned_data_for_step = lambda step: {
            "invoice_mode": "percentage",
            "percentage": Decimal("100"),
            "_calculated_subtotal": calculated_subtotal,
        }
        return view._build_items_initial()

    def test_zero_value_sources_kept_as_zero_lines(self):
        items = self._build_items()
        self.assertEqual(self._source_ids(items), self._expected_source_ids())
        by_source = {i["source_id"]: i for i in items}
        for src in self._expected_source_ids() - {f"config_{self.config_normal.pk}"}:
            self.assertEqual(by_source[src]["unit_price"], Decimal("0.00"), src)

    def test_zero_lines_do_not_change_invoiced_total(self):
        items = self._build_items()
        total = sum(i["unit_price"] * i["quantity"] for i in items)
        self.assertEqual(total, Decimal("100.00"))

    def test_zero_ratio_yields_no_lines(self):
        """Nothing left to invoice → no prefilled lines (previous behavior kept)."""
        self.assertEqual(self._build_items(calculated_subtotal=Decimal("0.00")), [])


class ApiBuilderZeroValueItemsTests(_ZeroValueItemsTestBase):
    """build_items_from_summary used by the mobile/API invoicing flow."""

    def _build_items(self, summary=None):
        from api.billing.serializers import build_items_from_summary
        return build_items_from_summary(
            summary or self.summary, "percentage", Decimal("100"), None
        )

    def test_zero_value_sources_kept_as_zero_lines(self):
        items = self._build_items()
        self.assertEqual(self._source_ids(items), self._expected_source_ids())
        by_source = {i["source_id"]: i for i in items}
        zero_config_line = by_source[f"config_{self.config_zero.pk}"]
        self.assertEqual(zero_config_line["unit_price"], Decimal("0.00"))
        self.assertEqual(zero_config_line["description"], "Puerta gratis")

    def test_zero_remaining_yields_no_lines(self):
        summary = {**self.summary, "remaining_subtotal": Decimal("0.00")}
        self.assertEqual(self._build_items(summary), [])


class BulkZeroValueItemsTests(_ZeroValueItemsTestBase):
    """_build_items_for_pf and full bulk invoice creation."""

    def test_zero_value_sources_kept_as_zero_lines(self):
        items = BulkProjectFileInvoiceService._build_items_for_pf(
            self.project_file, self.summary
        )
        self.assertEqual(self._source_ids(items), self._expected_source_ids())

    def test_bulk_invoice_totals_unaffected_by_zero_lines(self):
        invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 7, 14),
        )
        self.assertEqual(invoice.items.count(), 5)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("21.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))


class WizardServiceZeroLineTests(_ZeroValueItemsTestBase):
    """End to end: creating the invoice with 0 € lines keeps totals right."""

    def test_invoice_created_with_zero_lines(self):
        items_data = [
            {
                "source_id": f"config_{self.config_normal.pk}",
                "description": "Puerta",
                "quantity": Decimal("1"),
                "unit_price": Decimal("100.00"),
                "discount_percentage": Decimal("0.00"),
            },
            {
                "source_id": f"config_{self.config_zero.pk}",
                "description": "Puerta gratis",
                "quantity": Decimal("1"),
                "unit_price": Decimal("0.00"),
                "discount_percentage": Decimal("0.00"),
            },
            {
                "source_id": f"inv_item_{self.inv_item_zero.pk}",
                "description": "Regalo",
                "quantity": Decimal("2"),
                "unit_price": Decimal("0.00"),
                "discount_percentage": Decimal("0.00"),
            },
        ]
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 7, 14),
            due_date=date(2026, 8, 14),
            items_data=items_data,
        )
        self.assertEqual(invoice.items.count(), 3)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("21.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))
