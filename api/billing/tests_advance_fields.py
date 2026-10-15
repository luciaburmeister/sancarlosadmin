"""
What the mobile API exposes about advance invoices:

- sales-invoice detail and list: ``document_type`` and ``advance_percentage``;
  per detail line ``is_advance_deduction`` and ``advance_invoice_number``;
- project-file detail: ``invoicing.advances_invoiced`` and
  ``invoicing.advances_pending`` (taxable bases), ``document_type`` and
  ``advance_percentage`` on its ``sales_invoices``;
- project-file detail ``can_create`` and bulk-emit ``facturable`` are false
  while an advance is still a pro forma, or its cancellation at the AEAT is
  in flight, since its goods cannot be invoiced.
"""
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from api.billing.serializers import SalesInvoiceDetailSerializer
from apps.einvoicing.models import SubmissionStatus
from invoices.models import SalesInvoice, SalesInvoiceDocumentType, SalesInvoiceStatus
from invoices.tests.advance_fixtures import AdvanceApiMixin, aeat_cancel_marker
from sales.models import Quote, QuoteStatus


class _AdvanceFieldsBase(AdvanceApiMixin, TestCase):
    api_username = "advance-fields"

    def _get(self, url):
        response = self.api.get(url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _issued_advance(self, base=Decimal("300.00"), percentage="30", number="INV-2026-00012"):
        """A FINAL advance with its invoice number. Returns (invoice, line)."""
        advance, line = self.make_advance(base, percentage)
        SalesInvoice.objects.filter(pk=advance.pk).update(invoice_number=number)
        advance.refresh_from_db()
        line.refresh_from_db()
        return advance, line


class SalesInvoiceFieldsTests(_AdvanceFieldsBase):
    def _detail(self, invoice):
        return self._get(f"/es/api/sales-invoices/{invoice.pk}/")

    def test_advance_invoice(self):
        advance, line = self._issued_advance()

        data = self._detail(advance)

        self.assertEqual(data["document_type"], SalesInvoiceDocumentType.ADVANCE)
        self.assertEqual(data["advance_percentage"], 30.0)
        [item] = data["items"]
        self.assertEqual(item["id"], line.pk)
        self.assertFalse(item["is_advance_deduction"])
        self.assertIsNone(item["advance_invoice_number"])

    def test_invoice_deducting_an_advance(self):
        _, line = self._issued_advance()
        rest = self.invoice_goods()
        deduction = self.deduct(rest, line, Decimal("300.00"))

        data = self._detail(rest)

        self.assertEqual(data["document_type"], SalesInvoiceDocumentType.INVOICE)
        self.assertIsNone(data["advance_percentage"])
        items = {item["id"]: item for item in data["items"]}
        self.assertTrue(items[deduction.pk]["is_advance_deduction"])
        self.assertEqual(items[deduction.pk]["advance_invoice_number"], "INV-2026-00012")
        self.assertEqual(items[deduction.pk]["line_total"], -300.0)
        goods = [item for pk, item in items.items() if pk != deduction.pk]
        self.assertTrue(goods)
        for item in goods:
            self.assertFalse(item["is_advance_deduction"])
            self.assertIsNone(item["advance_invoice_number"])

    def test_advance_numbers_take_no_query_per_deduction(self):
        _, first = self._issued_advance()
        _, second = self._issued_advance(Decimal("200.00"), "20", number="INV-2026-00013")
        rest = self.invoice_goods()
        self.deduct(rest, first, Decimal("300.00"))

        def queries():
            with CaptureQueriesContext(connection) as context:
                data = SalesInvoiceDetailSerializer(SalesInvoice.objects.get(pk=rest.pk)).data
            return len(context), data

        one, _ = queries()
        self.deduct(rest, second, Decimal("200.00"))
        two, data = queries()

        self.assertEqual(two, one)
        self.assertCountEqual(
            [item["advance_invoice_number"] for item in data["items"] if item["is_advance_deduction"]],
            ["INV-2026-00012", "INV-2026-00013"],
        )

    def test_list(self):
        advance, _ = self._issued_advance()
        rest = self.invoice_goods()

        results = {row["id"]: row for row in self._get("/es/api/sales-invoices/")["results"]}

        self.assertEqual(results[advance.pk]["document_type"], SalesInvoiceDocumentType.ADVANCE)
        self.assertEqual(results[advance.pk]["advance_percentage"], 30.0)
        self.assertEqual(results[rest.pk]["document_type"], SalesInvoiceDocumentType.INVOICE)
        self.assertIsNone(results[rest.pk]["advance_percentage"])


class ProjectFileInvoicingTests(_AdvanceFieldsBase):
    def _detail(self):
        return self._get(f"/es/api/project-files/{self.project_file.pk}/")

    def test_without_advances(self):
        invoicing = self._detail()["invoicing"]

        self.assertEqual(invoicing["advances_invoiced"], 0.0)
        self.assertEqual(invoicing["advances_pending"], 0.0)
        self.assertTrue(invoicing["can_create"])
        self.assertIsNone(invoicing["block_reason"])

    def test_advance_pending_until_deducted(self):
        _, line = self._issued_advance()

        invoicing = self._detail()["invoicing"]
        self.assertEqual(invoicing["advances_invoiced"], 300.0)
        self.assertEqual(invoicing["advances_pending"], 300.0)
        self.assertTrue(invoicing["can_create"])

        self.deduct(self.invoice_goods(), line, Decimal("300.00"))

        invoicing = self._detail()["invoicing"]
        self.assertEqual(invoicing["advances_invoiced"], 300.0)
        self.assertEqual(invoicing["advances_pending"], 0.0)
        self.assertFalse(invoicing["can_create"])

    def test_partly_deducted_advance(self):
        _, line = self._issued_advance()
        self.deduct(self.invoice_goods(Decimal("0.5")), line, Decimal("150.00"))

        invoicing = self._detail()["invoicing"]

        self.assertEqual(invoicing["advances_invoiced"], 300.0)
        self.assertEqual(invoicing["advances_pending"], 150.0)
        self.assertTrue(invoicing["can_create"])

    def test_cancelled_advance_is_left_out(self):
        advance, _ = self._issued_advance()
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        invoicing = self._detail()["invoicing"]

        self.assertEqual(invoicing["advances_invoiced"], 0.0)
        self.assertEqual(invoicing["advances_pending"], 0.0)

    def test_proforma_advance_blocks_goods_invoicing(self):
        advance, _ = self.make_advance(
            Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA,
        )

        invoicing = self._detail()["invoicing"]

        self.assertFalse(invoicing["can_create"])
        self.assertIn(advance.display_number, invoicing["block_reason"])
        # It counts toward the advances all the same
        self.assertEqual(invoicing["advances_pending"], 300.0)

    def test_advance_being_cancelled_blocks_goods_invoicing(self):
        advance, _ = self._issued_advance()
        for status in (SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED):
            with self.subTest(status=status):
                marker = aeat_cancel_marker(advance, status=status)

                invoicing = self._detail()["invoicing"]

                self.assertFalse(invoicing["can_create"])
                self.assertIn(advance.invoice_number, invoicing["block_reason"])
                self.assertEqual(invoicing["advances_pending"], 300.0)
                marker.delete()

    def test_answered_cancellation_does_not_block_it(self):
        advance, _ = self._issued_advance()
        marker = aeat_cancel_marker(advance, status=SubmissionStatus.PENDING)
        for status in (SubmissionStatus.REJECTED, SubmissionStatus.ERROR):
            with self.subTest(status=status):
                marker.status = status
                marker.save(update_fields=["status"])

                invoicing = self._detail()["invoicing"]

                self.assertTrue(invoicing["can_create"])
                self.assertIsNone(invoicing["block_reason"])

    def test_sales_invoices_carry_their_document_type(self):
        advance, _ = self._issued_advance()
        rest = self.invoice_goods()

        invoices = {inv["id"]: inv for inv in self._detail()["sales_invoices"]}

        self.assertEqual(invoices[advance.pk]["document_type"], SalesInvoiceDocumentType.ADVANCE)
        self.assertEqual(invoices[advance.pk]["advance_percentage"], 30.0)
        self.assertEqual(invoices[rest.pk]["document_type"], SalesInvoiceDocumentType.INVOICE)
        self.assertIsNone(invoices[rest.pk]["advance_percentage"])


class BulkEmitCandidateTests(_AdvanceFieldsBase):
    def _candidate(self):
        [candidate] = [
            c for c in self._get("/es/api/sales-invoices/bulk-emit-candidates/")["results"]
            if c["id"] == self.project_file.pk
        ]
        return candidate

    def test_proforma_advance_is_not_facturable(self):
        advance, _ = self.make_advance(
            Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA,
        )

        candidate = self._candidate()

        self.assertFalse(candidate["facturable"])
        self.assertIn(advance.display_number, candidate["block_reason"])
        self.assertEqual(candidate["accepted_remaining"], 1000.0)
        self.assertEqual(candidate["advances_open_net"], 300.0)

    def test_proforma_advance_with_pending_quotes_only(self):
        # Bulk-emit accepts the sent quote and is then refused all the same
        advance, _ = self.make_advance(
            Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA,
        )
        Quote.objects.filter(pk=self.quote.pk).update(status=QuoteStatus.SENT)

        candidate = self._candidate()

        self.assertFalse(candidate["facturable"])
        self.assertIn(advance.display_number, candidate["block_reason"])
        self.assertEqual(candidate["pending_quote_count"], 1)
        self.assertEqual(candidate["accepted_remaining"], 0.0)
        self.assertEqual(candidate["advances_open_net"], 300.0)

    def test_advance_being_cancelled_is_not_facturable(self):
        advance, _ = self._issued_advance()
        for status in (SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED):
            with self.subTest(status=status):
                marker = aeat_cancel_marker(advance, status=status)

                candidate = self._candidate()

                self.assertFalse(candidate["facturable"])
                self.assertIn(advance.invoice_number, candidate["block_reason"])
                self.assertEqual(candidate["advances_open_net"], 300.0)
                marker.delete()
