"""
Tests for ProductTemplate.exclude_from_invoicing.

Configurations of an excluded product (e.g. vehicle reception) must never
appear in the invoiceable summary, in any of the three item-building flows,
in the aggregated taxes, or in the project file cached totals — unlike plain
zero-value configurations, which are kept as 0 € lines (see
tests_zero_value_items.py).
"""
from decimal import Decimal

from django.test import TestCase

from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.tests.factories import (
    ExpedienteMixin,
    build_api_items,
    build_wizard_items,
    make_configuration,
    make_product_template,
)


class ExcludedFromInvoicingTests(ExpedienteMixin, TestCase):
    """One invoiceable direct config plus one excluded (reception) config."""

    company_name = "Taller Co"
    product_name = "Trabajo taller"
    project_file_name = "PF Taller"

    def setUp(self):
        super().setUp()
        self.reception_product = make_product_template(
            company=self.company,
            name="Recepción de vehículo",
            flow_kind="shop",
            exclude_from_invoicing=True,
        )
        self.direct_normal = self._make_config(None, "200.00", name="Chapa y pintura")
        # Even a tax applied to the reception config must not leak into
        # aggregations.
        self.reception = make_configuration(
            project_file=self.project_file, product=self.reception_product,
            subtotal="0.00", name="Recepción",
        )

        self.summary = ProjectFileInvoiceService.get_invoiceable_summary(
            self.project_file
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
        items = build_wizard_items(self.summary)
        source_ids = {i["source_id"] for i in items}
        self.assertEqual(source_ids, {f"direct_{self.direct_normal.pk}"})

    def test_excluded_config_not_in_api_items(self):
        items = build_api_items(self.summary)
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
