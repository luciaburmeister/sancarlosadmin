"""A quote-bound Configuration whose product is flagged exclude_from_invoicing
never reaches an invoice: it is a price-only rate (e.g. a per-movement cuba
rate agreed up front) actually billed later through another source, such as
a WorkEntry per real movement."""
from decimal import Decimal

from django.test import TestCase

from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.tests.factories import (
    build_api_items,
    build_wizard_items,
    make_client,
    make_company,
    make_configuration,
    make_product_template,
    make_project_file,
    make_quote,
)


class ExcludeFromInvoicingQuoteConfigurationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="San Carlos Test")
        cls.client_obj = make_client(company=cls.company)
        cls.excluded_product = make_product_template(
            company=cls.company, name="CUBAS", exclude_from_invoicing=True,
        )
        cls.billable_product = make_product_template(company=cls.company, name="Otro")
        cls.project_file = make_project_file(
            company=cls.company, client=cls.client_obj, name="PF Cubas",
        )
        cls.quote = make_quote(project_file=cls.project_file, project_name="Presupuesto cubas")
        cls.excluded_config = make_configuration(
            product=cls.excluded_product, quote=cls.quote, subtotal="380.00", tax_pct=10,
        )
        cls.quote.recalc_totals()
        cls.quote.refresh_from_db()

    def _summary(self):
        return ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)

    def test_excluded_config_is_not_a_line(self):
        summary = self._summary()
        items = ProjectFileInvoiceService.build_items_for_ratio(summary, Decimal("1"))
        self.assertEqual(items, [])

    def test_excluded_config_leaves_nothing_invoiceable(self):
        summary = self._summary()
        self.assertEqual(summary["total_invoiceable_subtotal"], Decimal("0.00"))
        self.assertFalse(summary["can_invoice"])

    def test_wizard_items_step_is_also_empty(self):
        self.assertEqual(build_wizard_items(self._summary()), [])

    def test_api_builder_is_also_empty(self):
        self.assertEqual(build_api_items(self._summary()), [])

    def test_a_billable_configuration_on_the_same_quote_still_bills(self):
        config = make_configuration(
            product=self.billable_product, quote=self.quote, subtotal="100.00",
            tax_pct=10, name="Otro item",
        )
        self.quote.recalc_totals()
        self.quote.refresh_from_db()

        items = ProjectFileInvoiceService.build_items_for_ratio(self._summary(), Decimal("1"))

        self.assertEqual(
            [(i["source_id"], i["unit_price"]) for i in items],
            [(f"config_{config.pk}", Decimal("100.00"))],
        )
