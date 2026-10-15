"""
The expediente's cached invoicing figures with advance invoices
(ProjectFile.recalculate_total_amount, kept current by the invoice signals):

- advance_pending_amount: the advances' base (pro formas included) less
  what non-cancelled invoices have deducted;
- a payment (payment status save) leaves the expediente alone;
- a 100 % advance invoices 100 % but is not full invoicing until it is
  deducted, so it neither unlocks full-invoicing gates nor auto-completes
  the expediente;
- the admin never deletes an expediente that has an advance invoice, a
  cancelled one included (an advance always keeps its expediente).
"""
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from invoices.models import InvoicePaymentStatus, SalesInvoiceStatus
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from management.admin.project_file import ProjectFileAdmin
from management.models import (
    ProjectFile,
    ProjectFileDeliveryStatus,
    ProjectFileProductionStatus,
    ProjectFileStatus,
)
from management.services.project_file_status import compute_lifecycle_status


class ProjectFileAdvanceTotalsTests(AdvanceExpedienteMixin, TestCase):
    def _refreshed(self):
        self.project_file.refresh_from_db()
        return self.project_file

    def test_advance_pending_amount(self):
        self.assertEqual(self._refreshed().advance_pending_amount.amount, Decimal("0.00"))

        advance, line = self.make_advance(
            Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA,
        )
        self.assertEqual(self._refreshed().advance_pending_amount.amount, Decimal("300.00"))
        self.set_status(advance, SalesInvoiceStatus.FINAL)
        self.assertEqual(self._refreshed().advance_pending_amount.amount, Decimal("300.00"))
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("30.00"))

        rest = self.invoice_goods(Decimal("0.5"))
        self.deduct(rest, line, Decimal("150.00"))
        self.assertEqual(self._refreshed().advance_pending_amount.amount, Decimal("150.00"))
        # 300 advance + 500 goods − 150 deducted
        self.assertEqual(self.project_file.total_invoiced.amount, Decimal("650.00"))

    def test_cancelled_advance_is_not_pending(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)
        self.assertEqual(self._refreshed().advance_pending_amount.amount, Decimal("0.00"))
        self.assertEqual(self.project_file.total_invoiced.amount, Decimal("0.00"))


class PaymentStatusSaveTests(AdvanceExpedienteMixin, TestCase):
    def test_leaves_the_expediente_alone(self):
        """A payment moves nothing the expediente caches, and must not lock
        it after the invoice row (the invoicing paths lock it first)."""
        invoice = self.invoice_goods()
        invoice.payment_status = InvoicePaymentStatus.PAID

        with CaptureQueriesContext(connection) as ctx:
            invoice.save(update_fields=["payment_status"])

        self.assertEqual(
            [query["sql"] for query in ctx.captured_queries if '"management_projectfile"' in query["sql"]],
            [],
        )


class FullAdvanceIsNotFullInvoicingTests(AdvanceExpedienteMixin, TestCase):
    def _lifecycle(self):
        self.project_file.refresh_from_db()
        self.project_file.status = ProjectFileStatus.ACTIVE
        return compute_lifecycle_status(
            self.project_file,
            ProjectFileDeliveryStatus.DELIVERED,
            ProjectFileProductionStatus.NO_PRODUCTION,
        )

    def test_full_advance_until_deducted(self):
        _, line = self.make_advance(Decimal("1000.00"), "100")
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("100.00"))
        self.assertFalse(self.project_file.is_fully_invoiced)
        self.assertEqual(self._lifecycle(), ProjectFileStatus.ACTIVE)

        rest = self.invoice_goods()
        self.deduct(rest, line, Decimal("1000.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("100.00"))
        self.assertTrue(self.project_file.is_fully_invoiced)
        self.assertEqual(self._lifecycle(), ProjectFileStatus.COMPLETED)


class ProjectFileAdminDeleteTests(AdvanceExpedienteMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("admin", "a@x.com", "pw")
        self.client.force_login(self.user)

    def _can_delete(self, project_file):
        request = RequestFactory().get("/")
        request.user = self.user
        return ProjectFileAdmin(ProjectFile, admin.site).has_delete_permission(request, project_file)

    def _delete_selected(self, *project_files):
        return self.client.post(reverse("admin:management_projectfile_changelist"), {
            "action": "delete_selected",
            "_selected_action": [pf.pk for pf in project_files],
            "post": "yes",
        })

    def test_an_advance_takes_the_delete_permission_away(self):
        self.assertTrue(self._can_delete(self.project_file))
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        self.assertFalse(self._can_delete(self.project_file))
        response = self.client.get(
            reverse("admin:management_projectfile_delete", args=[self.project_file.pk])
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ProjectFile.objects.filter(pk=self.project_file.pk).exists())

    def test_delete_selected_names_the_advances_and_deletes_nothing(self):
        self.make_advance(Decimal("300.00"), "30")
        other = ProjectFile.objects.create(name="Otra", company=self.company, client=self.client_obj)

        response = self._delete_selected(self.project_file, other)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["protected"]), 1)
        self.assertEqual(ProjectFile.objects.filter(pk__in=[self.project_file.pk, other.pk]).count(), 2)

    def test_delete_selected_still_deletes_expedientes_without_advances(self):
        other = ProjectFile.objects.create(name="Otra", company=self.company, client=self.client_obj)

        response = self._delete_selected(other)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(ProjectFile.objects.filter(pk=other.pk).exists())
