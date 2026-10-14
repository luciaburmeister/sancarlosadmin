"""
Bulk invoices (one invoice over several expedientes) and the expediente
gates:

- an expediente whose goods are invoiced (bulk invoices included) is not
  billed again;
- each expediente is billed what it has pending, built like its own invoices
  (build_items_for_remaining, each configuration at what it has pending), and the net
  over-invoicing check runs per expediente;
- a bulk invoice closes every expediente it bills, so it deducts all their
  advances pending (not those of the expedientes it skips); its deduction
  lines carry their expediente, as its goods lines do, and are not goods;
- deleting a bulk pro forma recalculates every expediente it covered,
  advances pending included.
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from configurator.models import Configuration
from inventory.models import InventoryItem
from invoices.models import SalesInvoice
from invoices.services.advances import AdvanceDeductionService
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from sales.models import QuoteInventoryItem


class BulkInvoiceAdvanceTests(AdvanceExpedienteMixin, TestCase):
    def _bulk(self, project_files=None):
        return BulkProjectFileInvoiceService.create_bulk_invoice(
            project_files or [self.project_file],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )

    def test_bulk_invoiced_expediente_is_not_billed_again(self):
        self._bulk()
        with self.assertRaises(ValidationError):
            self._bulk()
        self.assertEqual(SalesInvoice.objects.filter(project_files=self.project_file).count(), 1)

    def test_bills_what_a_partial_invoice_left(self):
        self.invoice_goods(Decimal("0.5"))

        bulk = self._bulk()

        [line] = bulk.items.all()
        self.assertEqual(line.source_id, f"config_{self.config.pk}")
        self.assertEqual(line.line_total.amount, Decimal("500.00"))
        self.assertTrue(line.description.startswith(f"[{self.project_file.reference}] "))
        self.assertEqual(line.project_file, self.project_file)
        self.assertEqual(bulk.total_amount.amount, Decimal("605.00"))
        self.assertEqual(self.summary()["goods_remaining_net"], Decimal("0.00"))

    def test_bills_each_configuration_at_its_subtotal(self):
        """Its pricing lines need not add up to it: the BOM base (900.00)
        is no line of its own, only the 100.00 fitting charge is."""
        Configuration.objects.filter(pk=self.config.pk).update(pricing_breakdown={
            "base_subtotal": "900.00",
            "post_discount_subtotal": "1000.00",
            "lines": [{"label": "Montaje", "type": "charge", "amount": "100.00"}],
        })

        bulk = self._bulk()

        [line] = bulk.items.all()
        self.assertEqual(line.line_total.amount, Decimal("1000.00"))
        self.assertEqual(bulk.subtotal_amount.amount, Decimal("1000.00"))
        self.assertEqual(bulk.total_amount.amount, Decimal("1210.00"))
        self.assertFalse(self.summary()["can_invoice"])

    def test_inventory_item_billed_by_a_partial_invoice_is_not_billed_again(self):
        # Inventory items are billed in full on any partial invoice
        QuoteInventoryItem.objects.create(
            quote=self.quote,
            inventory_item=InventoryItem.objects.create(name="Tirador", company=self.company),
            quantity=Decimal("1"), unit_price=Decimal("200.00"),
        )
        self.quote.recalc_totals()
        self.invoice_goods(Decimal("0.5"))

        bulk = self._bulk()

        self.assertEqual(
            [line.source_id for line in bulk.items.all()], [f"config_{self.config.pk}"],
        )
        self.assertEqual(self.summary()["goods_remaining_net"], Decimal("0.00"))

    def test_net_check_refuses_over_billing(self):
        # A line with no source billed part of the expediente: its sources
        # still have their whole amount pending
        ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 1), due_date=None,
            items_data=[{"source_id": "", "description": "A cuenta", "unit_price": Decimal("600.00")}],
        )

        with self.assertRaises(ValidationError):
            self._bulk()
        self.assertFalse(SalesInvoice.objects.filter(project_file__isnull=True).exists())

    def test_deducts_every_advance_pending(self):
        _, line = self.make_advance(Decimal("300.00"), "30")

        bulk = self._bulk()

        [deduction] = AdvanceDeductionService.deduction_lines(bulk)
        self.assertEqual(deduction.advance_line, line)
        self.assertEqual(deduction.source_id, f"advance_ded_{line.pk}")
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))
        # Bulk lines say which expediente they bill
        self.assertEqual(deduction.project_file, self.project_file)
        self.assertEqual(
            (bulk.subtotal_amount.amount, bulk.tax_amount.amount, bulk.total_amount.amount),
            (Decimal("700.00"), Decimal("147.00"), Decimal("847.00")),
        )

        summary = self.summary()
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertEqual(summary["total_invoiced_subtotal"], Decimal("1000.00"))
        # 300 advance + 1000 goods − 300 deducted
        self.assertEqual(summary["total_invoiced_subtotal_net"], Decimal("1000.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("100.00"))

    def test_each_expediente_deducts_its_own_advances(self):
        other, _, _ = self.make_expediente("Obra 2", subtotal=Decimal("500.00"))
        _, line = self.make_advance(Decimal("300.00"), "30")

        bulk = self._bulk([self.project_file, other])

        [deduction] = AdvanceDeductionService.deduction_lines(bulk)
        self.assertEqual((deduction.advance_line, deduction.project_file), (line, self.project_file))
        # 1000 + 500 of goods − 300; IVA 21 %
        self.assertEqual(bulk.total_amount.amount, Decimal("1452.00"))
        other.refresh_from_db()
        self.assertEqual(other.percentage_invoiced, Decimal("100.00"))

    def test_an_expediente_it_skips_keeps_its_advance(self):
        # Goods invoiced without deducting its advance: nothing to bill
        self.make_advance(Decimal("300.00"), "30")
        self.invoice_goods()
        other, _, _ = self.make_expediente("Obra 2", subtotal=Decimal("500.00"))

        bulk = self._bulk([self.project_file, other])

        self.assertEqual(AdvanceDeductionService.deduction_lines(bulk), [])
        self.assertEqual(bulk.total_amount.amount, Decimal("605.00"))
        self.assertEqual(self.summary()["advances_open_net"], Decimal("300.00"))

    def test_deleting_a_bulk_proforma_recalculates_every_expediente(self):
        other, _, _ = self.make_expediente("Obra 2", subtotal=Decimal("500.00"))
        bulk = self._bulk([self.project_file, other])
        for project_file in (self.project_file, other):
            project_file.refresh_from_db()
            self.assertEqual(project_file.percentage_invoiced, Decimal("100.00"))

        bulk.delete()

        for project_file in (self.project_file, other):
            project_file.refresh_from_db()
            self.assertEqual(project_file.total_invoiced.amount, Decimal("0.00"))
            self.assertEqual(project_file.percentage_invoiced, Decimal("0.00"))

    def test_deleting_a_bulk_rest_proforma_restores_the_advance(self):
        self.make_advance(Decimal("300.00"), "30")
        bulk = self._bulk()
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))

        bulk.delete()

        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("300.00"))
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("30.00"))
