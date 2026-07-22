"""
Tests for ProductTemplate.exclude_from_invoicing.

Configurations of an excluded product (e.g. vehicle reception) must never
appear in the invoiceable summary, in any of the three item-building flows,
in the aggregated taxes, or in the project file cached totals — unlike plain
zero-value configurations, which are kept as 0 € lines (see
tests_zero_value_items.py).
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from company.models import Company
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from invoices.models import AppliedTax, TaxType
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile
from sales.models import Client


class ExcludedFromInvoicingTests(TestCase):
    """One invoiceable direct config plus one excluded (reception) config."""

    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(
            name="Trabajo taller", company=self.company
        )
        self.reception_product = ProductTemplate.objects.create(
            name="Recepción de vehículo",
            company=self.company,
            flow_kind="shop",
            exclude_from_invoicing=True,
        )
        self.project_file = ProjectFile.objects.create(
            name="PF Taller", company=self.company, client=self.client_obj,
        )
        self.iva21, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )

        self.direct_normal = self._create_direct_config(
            "Chapa y pintura", self.product, Decimal("200.00")
        )
        self.reception = self._create_direct_config(
            "Recepción", self.reception_product, Decimal("0.00")
        )
        # Even a tax applied to the reception config must not leak into
        # aggregations.
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=self.reception.pk,
            tax_type=self.iva21,
            selected_percentage=Decimal("21"),
            base_amount=Decimal("0.00"),
            calculated_amount=Decimal("0.00"),
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(Configuration),
            object_id=self.direct_normal.pk,
            tax_type=self.iva21,
            selected_percentage=Decimal("21"),
            base_amount=Decimal("200.00"),
            calculated_amount=Decimal("42.00"),
        )

        self.summary = ProjectFileInvoiceService.get_invoiceable_summary(
            self.project_file
        )

    def _create_direct_config(self, name, product, subtotal):
        return Configuration.objects.create(
            name=name,
            company=self.company,
            product=product,
            client=self.client_obj,
            project_file=self.project_file,
            status=ConfigurationStatus.SHOP,
            subtotal_price=subtotal,
            total_price=(subtotal * Decimal("1.21")).quantize(Decimal("0.01")),
        )

    def test_excluded_config_not_in_summary(self):
        ids = {c["id"] for c in self.summary["direct_configurations"]}
        self.assertIn(self.direct_normal.pk, ids)
        self.assertNotIn(self.reception.pk, ids)

    def test_excluded_config_does_not_affect_totals(self):
        self.assertEqual(
            self.summary["total_invoiceable_subtotal"], Decimal("200.00")
        )

    def test_excluded_config_not_in_wizard_items(self):
        from invoices.views.invoice.project_file_wizard_views import (
            ProjectFileInvoiceWizardView,
        )
        view = ProjectFileInvoiceWizardView(model_admin=None)
        view.invoicing_summary = self.summary
        view.get_cleaned_data_for_step = lambda step: {
            "invoice_mode": "percentage",
            "percentage": Decimal("100"),
            "_calculated_subtotal": self.summary["remaining_subtotal"],
        }
        items = view._build_items_initial()
        source_ids = {i["source_id"] for i in items}
        self.assertEqual(source_ids, {f"direct_{self.direct_normal.pk}"})

    def test_excluded_config_not_in_api_items(self):
        from api.billing.serializers import build_items_from_summary
        items = build_items_from_summary(
            self.summary, "percentage", Decimal("100"), None
        )
        source_ids = {i["source_id"] for i in items}
        self.assertEqual(source_ids, {f"direct_{self.direct_normal.pk}"})

    def test_excluded_config_not_in_bulk_items(self):
        items = BulkProjectFileInvoiceService._build_items_for_pf(
            self.project_file, self.summary
        )
        source_ids = {i["source_id"] for i in items}
        self.assertEqual(source_ids, {f"direct_{self.direct_normal.pk}"})

    def test_excluded_config_not_in_aggregated_taxes(self):
        taxes = ProjectFileInvoiceService.get_aggregated_quote_taxes(
            self.project_file
        )
        # Only the normal config's tax survives; the reception one is excluded
        # (they collapse to one 21% entry either way, but base comes from the
        # non-excluded config only).
        self.assertEqual(len(taxes), 1)

    def test_excluded_config_not_in_project_file_total(self):
        self.project_file.recalculate_total_amount()
        self.project_file.refresh_from_db()
        self.assertEqual(
            self.project_file.total_amount.amount, Decimal("200.00")
        )
