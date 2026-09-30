"""
The mobile API's invoicing gates with advance invoices: an advance does not
make an expediente invoiced, cancelled invoices bill nothing, and every gate
works on net goods figures.

- project-file detail: ``invoicing.can_create``;
- bulk-emit candidates: ``facturable``, ``accepted_remaining``,
  ``advances_open_net``;
- the project-file list's "in progress" / "invoiced" tabs;
- POST project-files/<id>/invoices/ after a 100 % advance;
- ``can_mark_completed``;
- POST project-files/<id>/invoices/ in advance mode: percentage or
  custom_amount required, percentage ≤ 100 in every mode;
- POST project-files/<id>/invoices/ deducts the advances (default plan);
  ``deduct_advances=false`` only when the invoice need not deduct any;
- bulk-emit deducts every advance pending;
- goods are not invoiced while an advance is still a pro forma, nor while
  its cancellation at the AEAT is in flight;
- POST quotes/<id>/invoice/ is refused while advances are pending
  deduction, with the error as plain text;
- an expediente with advances is not deleted, nor cancelled or completed
  while one is left to deduct; its quotes are not reverted to draft.
"""
import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from api._serializers import project_file_can_mark_completed
from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from company.models import Employee
from invoices.models import (
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceStatus,
)
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from management.models import ProjectFile, ProjectFileStatus
from sales.models import QuoteStatus


class _AdvanceApiBase(AdvanceExpedienteMixin, TestCase):
    def setUp(self):
        super().setUp()
        user = User.objects.create_user("advances", password="pw")
        Employee.objects.create(
            user=user, company=self.company, first_name="Ana", last_name="Anticipo",
            is_active=True, hired_at=date(2020, 1, 1),
        )
        self.api = APIClient()
        self.api.force_authenticate(user=user)

    def _mark_paid(self, *invoices):
        SalesInvoice.objects.filter(pk__in=[i.pk for i in invoices]).update(
            payment_status=InvoicePaymentStatus.PAID,
        )


class InvoicingGatesTests(_AdvanceApiBase):
    def _can_create(self):
        response = self.api.get(f"/es/api/project-files/{self.project_file.pk}/")
        self.assertEqual(response.status_code, 200)
        return response.json()["invoicing"]["can_create"]

    def _candidate(self):
        response = self.api.get("/es/api/sales-invoices/bulk-emit-candidates/")
        self.assertEqual(response.status_code, 200)
        [candidate] = [
            c for c in response.json()["results"] if c["id"] == self.project_file.pk
        ]
        return candidate

    def test_full_advance_leaves_the_goods_invoiceable(self):
        self.make_advance(Decimal("1000.00"), "100")

        self.assertTrue(self._can_create())
        candidate = self._candidate()
        self.assertTrue(candidate["facturable"])
        self.assertIsNone(candidate["block_reason"])
        self.assertEqual(candidate["accepted_remaining"], 1000.0)
        self.assertEqual(candidate["advances_open_net"], 1000.0)

    def test_invoiced_goods_close_the_gates(self):
        self.invoice_goods()

        self.assertFalse(self._can_create())
        candidate = self._candidate()
        self.assertFalse(candidate["facturable"])
        self.assertEqual(candidate["accepted_remaining"], 0.0)

    def test_cancelled_invoice_reopens_the_gates(self):
        self.set_status(self.invoice_goods(), SalesInvoiceStatus.CANCELLED)

        self.assertTrue(self._can_create())
        self.assertTrue(self._candidate()["facturable"])

    def test_invoice_can_be_created_after_a_full_advance(self):
        self.make_advance(Decimal("1000.00"), "100")

        response = self.api.post(
            f"/es/api/project-files/{self.project_file.pk}/invoices/",
            data=json.dumps({"invoice_mode": "percentage", "percentage": "100"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.json())
        invoice = SalesInvoice.objects.get(pk=response.json()["id"])
        # The goods in full, less the whole advance
        goods = invoice.items.get(advance_line__isnull=True)
        deduction = invoice.items.get(advance_line__isnull=False)
        self.assertEqual(goods.line_total.amount, Decimal("1000.00"))
        self.assertEqual(deduction.line_total.amount, Decimal("-1000.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("0.00"))


class InvoicedTabTests(_AdvanceApiBase):
    def _tab_ids(self, status):
        response = self.api.get(f"/es/api/project-files/?status={status}")
        self.assertEqual(response.status_code, 200)
        return [pf["id"] for pf in response.json()["results"]]

    def _assert_tab(self, tab):
        other = {"in_progress": "invoiced", "invoiced": "in_progress"}[tab]
        self.assertIn(self.project_file.pk, self._tab_ids(tab))
        self.assertNotIn(self.project_file.pk, self._tab_ids(other))

    def test_advance_alone_stays_in_progress(self):
        self.make_advance(Decimal("300.00"), "30")
        self._assert_tab("in_progress")

    def test_cancelled_invoice_stays_in_progress(self):
        self.set_status(self.invoice_goods(), SalesInvoiceStatus.CANCELLED)
        self._assert_tab("in_progress")

    def test_goods_invoice_is_invoiced(self):
        self.invoice_goods()
        self._assert_tab("invoiced")


class CanMarkCompletedTests(_AdvanceApiBase):
    def _can_mark_completed(self):
        return project_file_can_mark_completed(ProjectFile.objects.get(pk=self.project_file.pk))

    def test_paid_full_advance_alone_is_not_enough(self):
        advance, _ = self.make_advance(Decimal("1000.00"), "100")
        self._mark_paid(advance)
        self.assertFalse(self._can_mark_completed())

    def test_advance_left_to_deduct_blocks_it(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods()
        self._mark_paid(advance, rest)
        self.assertFalse(self._can_mark_completed())

    def test_advance_deducted_on_the_rest(self):
        advance, line = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods()
        self.deduct(rest, line, Decimal("300.00"))
        self.assertFalse(self._can_mark_completed())  # neither is paid yet

        self._mark_paid(advance, rest)
        self.assertTrue(self._can_mark_completed())

    def test_cancelled_unpaid_advance_does_not_block_it(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)
        self._mark_paid(self.invoice_goods())
        self.assertTrue(self._can_mark_completed())


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class AdvanceModeTests(_AdvanceApiBase):
    def _post(self, **data):
        return self.api.post(
            f"/es/api/project-files/{self.project_file.pk}/invoices/",
            data=json.dumps(data),
            content_type="application/json",
        )

    def _advance(self):
        return SalesInvoice.objects.get(document_type=SalesInvoiceDocumentType.ADVANCE)

    def test_percentage(self):
        response = self._post(invoice_mode="advance", percentage="30")

        self.assertEqual(response.status_code, 201, response.json())
        advance = self._advance()
        self.assertEqual(response.json()["id"], advance.pk)
        self.assertEqual(advance.status, SalesInvoiceStatus.PROFORMA)
        self.assertEqual(advance.project_file, self.project_file)
        self.assertEqual(advance.advance_percentage, Decimal("30.0000"))
        self.assertEqual(response.json()["subtotal_amount"], 300.0)
        self.assertEqual(response.json()["total_amount"], 363.0)

    def test_custom_amount_is_the_advance_base(self):
        response = self._post(invoice_mode="advance", custom_amount="250.00")

        self.assertEqual(response.status_code, 201, response.json())
        advance = self._advance()
        self.assertEqual(advance.subtotal_amount.amount, Decimal("250.00"))
        self.assertEqual(advance.advance_percentage, Decimal("25.0000"))

    def test_finalize(self):
        response = self._post(invoice_mode="advance", percentage="30", finalize=True)

        self.assertEqual(response.status_code, 201, response.json())
        advance = self._advance()
        self.assertEqual(advance.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(response.json()["invoice_number"], advance.invoice_number)

    def test_percentage_or_amount_is_required(self):
        # The serializer's percentage default (100) must not issue a 100 % advance
        response = self._post(invoice_mode="advance")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_invalid_input_is_refused(self):
        for data in (
            {"invoice_mode": "advance", "percentage": "0"},
            {"invoice_mode": "advance", "percentage": "100.5"},
            {"invoice_mode": "advance", "custom_amount": "0"},
            {"invoice_mode": "advance", "custom_amount": "1000.01"},
            {"invoice_mode": "percentage", "percentage": "150"},
        ):
            with self.subTest(**data):
                self.assertEqual(self._post(**data).status_code, 400)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_cap_includes_proforma_advances(self):
        self.assertEqual(self._post(invoice_mode="advance", percentage="60").status_code, 201)

        self.assertEqual(self._post(invoice_mode="advance", percentage="60").status_code, 400)
        self.assertEqual(
            SalesInvoice.objects.filter(document_type=SalesInvoiceDocumentType.ADVANCE).count(), 1,
        )


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class DeductAdvancesTests(_AdvanceApiBase):
    def setUp(self):
        super().setUp()
        _, self.line = self.make_advance(Decimal("300.00"), "30")

    def _post(self, **data):
        return self.api.post(
            f"/es/api/project-files/{self.project_file.pk}/invoices/",
            data=json.dumps(data),
            content_type="application/json",
        )

    def _deductions(self, invoice):
        return [
            (line.advance_line, line.line_total.amount)
            for line in invoice.items.filter(advance_line__isnull=False)
        ]

    def test_deducted_by_default(self):
        response = self._post(invoice_mode="percentage", percentage="100")

        self.assertEqual(response.status_code, 201, response.json())
        invoice = SalesInvoice.objects.get(pk=response.json()["id"])
        self.assertEqual(self._deductions(invoice), [(self.line, Decimal("-300.00"))])
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))

    def test_closing_invoice_cannot_skip_the_deduction(self):
        response = self._post(invoice_mode="percentage", percentage="100", deduct_advances=False)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            SalesInvoice.objects.filter(document_type=SalesInvoiceDocumentType.INVOICE).exists()
        )

    def test_partial_invoice_can_skip_the_deduction(self):
        response = self._post(invoice_mode="percentage", percentage="50", deduct_advances=False)

        self.assertEqual(response.status_code, 201, response.json())
        invoice = SalesInvoice.objects.get(pk=response.json()["id"])
        self.assertEqual(self._deductions(invoice), [])
        self.assertEqual(invoice.total_amount.amount, Decimal("605.00"))

    def test_bulk_emit_deducts_every_advance_pending(self):
        response = self.api.post(
            "/es/api/sales-invoices/bulk-emit/",
            data=json.dumps({"project_file_ids": [self.project_file.pk], "send_email": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.json())
        [success] = response.json()["successes"]
        invoice = SalesInvoice.objects.get(pk=success["invoice_id"])
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(self._deductions(invoice), [(self.line, Decimal("-300.00"))])
        self.assertEqual(success["total_amount"], 847.0)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class ProformaAdvanceTests(_AdvanceApiBase):
    def setUp(self):
        super().setUp()
        self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

    def _goods_invoiced(self):
        return SalesInvoice.objects.filter(document_type=SalesInvoiceDocumentType.INVOICE).exists()

    def test_goods_invoice_is_refused(self):
        response = self.api.post(
            f"/es/api/project-files/{self.project_file.pk}/invoices/",
            data=json.dumps({"invoice_mode": "percentage", "percentage": "100"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(self._goods_invoiced())

    def test_bulk_emit_reports_it(self):
        response = self.api.post(
            "/es/api/sales-invoices/bulk-emit/",
            data=json.dumps({"project_file_ids": [self.project_file.pk], "send_email": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        [failure] = response.json()["failures"]
        self.assertEqual(failure["project_file_id"], self.project_file.pk)
        self.assertFalse(self._goods_invoiced())

    def test_another_advance_can_still_be_issued(self):
        response = self.api.post(
            f"/es/api/project-files/{self.project_file.pk}/invoices/",
            data=json.dumps({"invoice_mode": "advance", "percentage": "10"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.json())


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class CancellingAdvanceTests(_AdvanceApiBase):
    """A final advance of 300.00 whose cancellation at the AEAT is in flight."""

    def setUp(self):
        super().setUp()
        self.advance, self.line = self.make_advance(Decimal("300.00"), "30")
        self.marker = EInvoicingSubmission.objects.create(
            account=EInvoicingAccount.objects.create(company=self.company, name="Verifacti"),
            sales_invoice=self.advance, action=SubmissionAction.CANCEL,
            status=SubmissionStatus.PENDING,
        )

    def _goods_invoices(self):
        return SalesInvoice.objects.filter(document_type=SalesInvoiceDocumentType.INVOICE)

    def _create(self):
        return self.api.post(
            f"/es/api/project-files/{self.project_file.pk}/invoices/",
            data=json.dumps({"invoice_mode": "percentage", "percentage": "100"}),
            content_type="application/json",
        )

    def _bulk_emit(self):
        return self.api.post(
            "/es/api/sales-invoices/bulk-emit/",
            data=json.dumps({"project_file_ids": [self.project_file.pk], "send_email": False}),
            content_type="application/json",
        )

    def test_goods_invoice_is_refused(self):
        for status in (SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED):
            with self.subTest(status=status):
                self.marker.status = status
                self.marker.save(update_fields=["status"])

                response = self._create()

                self.assertEqual(response.status_code, 400)
                self.assertIn(self.advance.display_number, json.dumps(response.json()))
                self.assertFalse(self._goods_invoices().exists())

    def test_bulk_emit_reports_it(self):
        for status in (SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED):
            with self.subTest(status=status):
                self.marker.status = status
                self.marker.save(update_fields=["status"])

                response = self._bulk_emit()

                self.assertEqual(response.status_code, 400)
                [failure] = response.json()["failures"]
                self.assertEqual(failure["project_file_id"], self.project_file.pk)
                self.assertIn(self.advance.display_number, failure["reason"])
                self.assertFalse(self._goods_invoices().exists())

    def test_once_rejected_the_goods_invoice_deducts_it(self):
        self.marker.status = SubmissionStatus.REJECTED
        self.marker.save(update_fields=["status"])

        response = self._create()

        self.assertEqual(response.status_code, 201, response.json())
        invoice = self._goods_invoices().get()
        self.assertEqual(
            [(line.advance_line, line.line_total.amount)
             for line in invoice.items.filter(advance_line__isnull=False)],
            [(self.line, Decimal("-300.00"))],
        )


class QuoteInvoiceTests(_AdvanceApiBase):
    def test_refused_while_an_advance_is_pending(self):
        self.make_advance(Decimal("300.00"), "30")

        response = self.api.post(f"/es/api/quotes/{self.quote.pk}/invoice/")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(SalesInvoice.objects.filter(configuration=self.config).exists())
        # Its messages, not the repr of their list
        error = response.json()["error"]
        self.assertFalse(error.startswith("["))
        self.assertNotIn("\\xa0", error)
        self.assertIn(self.project_file.reference or self.project_file.name, error)


class ExpedienteGuardsTests(_AdvanceApiBase):
    def _url(self, project_file=None):
        return f"/es/api/project-files/{(project_file or self.project_file).pk}/"

    def _patch_status(self, status):
        return self.api.patch(
            self._url(), data=json.dumps({"status": status}), content_type="application/json",
        )

    def _status(self):
        return ProjectFile.objects.get(pk=self.project_file.pk).status

    def test_cancel_and_complete_are_refused_while_an_advance_is_pending(self):
        self.make_advance(Decimal("300.00"), "30")
        before = self._status()

        for status in (ProjectFileStatus.CANCELLED, ProjectFileStatus.COMPLETED):
            with self.subTest(status=status):
                self.assertEqual(self._patch_status(status).status_code, 400)
                self.assertEqual(self._status(), before)

    def test_complete_once_the_advance_is_deducted(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        self.deduct(self.invoice_goods(), line, Decimal("300.00"))

        self.assertEqual(self._patch_status(ProjectFileStatus.COMPLETED).status_code, 200)
        self.assertEqual(self._status(), ProjectFileStatus.COMPLETED)

    def test_other_status_changes_are_not_affected(self):
        self.make_advance(Decimal("300.00"), "30")

        self.assertEqual(self._patch_status(ProjectFileStatus.ON_HOLD).status_code, 200)
        self.assertEqual(self._status(), ProjectFileStatus.ON_HOLD)

    def test_delete_is_refused_with_an_advance(self):
        # A cancelled advance too: an advance always keeps its expediente
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        self.assertEqual(self.api.delete(self._url()).status_code, 400)
        self.assertTrue(ProjectFile.objects.filter(pk=self.project_file.pk).exists())

    def test_delete_without_advances(self):
        other = ProjectFile.objects.create(name="Otra", company=self.company, client=self.client_obj)

        self.assertEqual(self.api.delete(self._url(other)).status_code, 200)
        self.assertFalse(ProjectFile.objects.filter(pk=other.pk).exists())


class QuoteRevertTests(_AdvanceApiBase):
    def _revert(self):
        return self.api.patch(
            f"/es/api/quotes/{self.quote.pk}/",
            data=json.dumps({"status": QuoteStatus.DRAFT}),
            content_type="application/json",
        )

    def test_refused_while_the_expediente_has_an_advance(self):
        self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

        self.assertEqual(self._revert().status_code, 400)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.ACCEPTED)

    def test_a_cancelled_advance_does_not_prevent_it(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        self.assertEqual(self._revert().status_code, 200)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, QuoteStatus.DRAFT)
