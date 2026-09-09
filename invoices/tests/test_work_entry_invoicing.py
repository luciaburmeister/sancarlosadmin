"""Work entries become invoice lines (wizard, API builder, bulk) and lock once invoiced."""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.test import TestCase
from djmoney.money import Money

from company.models import Company
from invoices.models import AppliedTax, TaxType
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile, WorkEntry, WorkEntryType
from sales.models import Client


class _WorkEntryInvoicingBase(TestCase):
    """One project file with a single 2 × 50 € work entry taxed at 21 %."""

    def setUp(self):
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.project_file = ProjectFile.objects.create(
            name="PF Work", company=self.company, client=self.client_obj,
        )
        self.entry_type = WorkEntryType.objects.create(company=self.company, name="Porte")
        self.iva21 = TaxType.objects.create(tax_name="IVA", country="ES", percentage_options=[21])

        self.entry = WorkEntry.objects.create(
            company=self.company,
            project_file=self.project_file,
            entry_type=self.entry_type,
            description="Porte Madrid",
            quantity=Decimal("2"),
            unit_price=Money("50.00", "EUR"),
            date=date(2026, 9, 1),
        )
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(WorkEntry),
            object_id=self.entry.pk,
            tax_type=self.iva21,
            selected_percentage=Decimal("21"),
            base_amount=Decimal("100.00"),
            calculated_amount=Decimal("21.00"),
        )
        self.source_id = f"work_entry_{self.entry.pk}"
        self.summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)

    def _line(self, unit_price, quantity=Decimal("2")):
        return {
            "source_id": self.source_id,
            "description": "Porte Madrid",
            "quantity": quantity,
            "unit_price": unit_price,
            "discount_percentage": Decimal("0.00"),
        }

    def _create_invoice(self, items_data, day=1):
        return ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file,
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, day),
            due_date=date(2026, 10, day),
            items_data=items_data,
        )


class SummaryTests(_WorkEntryInvoicingBase):
    def test_totals_split_subtotal_and_tax(self):
        self.assertEqual(self.summary["total_invoiceable_subtotal"], Decimal("100.00"))
        self.assertEqual(self.summary["total_invoiceable"], Decimal("121.00"))
        entry_data = self.summary["work_entries"][0]
        self.assertEqual(entry_data["total"], Decimal("100.00"))
        self.assertEqual(entry_data["tax_amount"], Decimal("21.00"))
        self.assertFalse(entry_data["is_locked"])


class LineBuilderTests(_WorkEntryInvoicingBase):
    def test_wizard_emits_work_entry_line(self):
        from invoices.views.invoice.project_file_wizard_views import ProjectFileInvoiceWizardView

        view = ProjectFileInvoiceWizardView(model_admin=None)
        view.invoicing_summary = self.summary
        view.get_cleaned_data_for_step = lambda step: {
            "invoice_mode": "percentage",
            "percentage": Decimal("100"),
            "_calculated_subtotal": self.summary["remaining_subtotal"],
        }
        items = view._build_items_initial()
        self.assertEqual(len(items), 1)
        line = items[0]
        self.assertEqual(line["source_id"], self.source_id)
        self.assertEqual(line["quantity"], Decimal("2"))
        self.assertEqual(line["unit_price"], Decimal("50.00"))
        self.assertEqual(line["_tax_amount"], Decimal("21.00"))

    def test_api_builder_emits_work_entry_line(self):
        from api.billing.serializers import build_items_from_summary

        items = build_items_from_summary(self.summary, "percentage", Decimal("50"), None)
        self.assertEqual([i["source_id"] for i in items], [self.source_id])
        self.assertEqual(items[0]["unit_price"], Decimal("50.00"))

    def test_bulk_builder_emits_work_entry_line(self):
        items = BulkProjectFileInvoiceService._build_items_for_pf(self.project_file, self.summary)
        self.assertEqual([i["source_id"] for i in items], [self.source_id])
        self.assertEqual(items[0]["quantity"], Decimal("2"))
        self.assertEqual(items[0]["unit_price"], Decimal("50.00"))
        self.assertEqual(items[0]["project_file_id"], self.project_file.pk)


class CreateInvoiceTests(_WorkEntryInvoicingBase):
    def test_full_invoice_uses_entry_taxes_and_locks_it(self):
        invoice = self._create_invoice([self._line(Decimal("50.00"))])
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.tax_amount.amount, Decimal("21.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))

        self.entry.refresh_from_db()
        self.assertTrue(self.entry.is_locked)

        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        self.assertEqual(summary["remaining"], Decimal("0.00"))
        self.assertEqual(summary["percentage_invoiced"], Decimal("100.00"))

    def test_bulk_invoice_locks_entry(self):
        invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file],
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 1),
        )
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.is_locked)

    def test_over_invoicing_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._create_invoice([self._line(Decimal("70.00"))])


class LockTests(_WorkEntryInvoicingBase):
    def setUp(self):
        super().setUp()
        self.first_invoice = self._create_invoice([self._line(Decimal("25.00"))], day=1)
        self.entry.refresh_from_db()

    def test_locked_entry_rejects_pricing_changes(self):
        self.assertTrue(self.entry.is_locked)
        self.entry.quantity = Decimal("3")
        with self.assertRaises(ValidationError):
            self.entry.full_clean()

    def test_locked_entry_allows_non_pricing_changes(self):
        self.entry.notes = "Confirmed with client"
        self.entry.full_clean()  # no error

    def test_unlocks_only_when_no_invoice_references_it(self):
        second_invoice = self._create_invoice([self._line(Decimal("25.00"))], day=2)

        self.first_invoice.delete()
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.is_locked, "still referenced by the second invoice")

        second_invoice.delete()
        self.entry.refresh_from_db()
        self.assertFalse(self.entry.is_locked)
