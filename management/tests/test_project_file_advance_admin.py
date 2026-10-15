"""
The expediente admin with advance invoices:

- the sales invoices inline labels each invoice with its type, and its
  "This PF" column shows the whole invoice for the advance (554.06) and for
  the rest invoice (1,292.81) of the user's worked example;
- the "Invoicing" list column shows advances not deducted yet apart
  ("+30% advance") from the goods invoiced;
- the invoicing summary adds Advanced / Deducted / Pending to deduct
  tiles and one row per advance, flagging unpaid advances and an advance
  no goods are left to deduct from;
- the form refuses to complete or cancel it while an advance is pending.
"""
import re
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils.translation import gettext

from invoices.models import SalesInvoice, SalesInvoiceStatus
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin, UserExpedienteMixin
from management.admin.project_file import ProjectFileAdmin, SalesInvoicesInline
from management.forms import ProjectFileAdminForm
from management.models import ProjectFile, ProjectFileStatus

# The "+N% advance" part of the Invoicing column
ADVANCE_PART = re.compile(r"\+\s*\d")


class SalesInvoicesInlineTests(UserExpedienteMixin, TestCase):
    """The worked example: a 30 % advance, then "100 % del pendiente"."""

    def setUp(self):
        super().setUp()
        self.advance, self.rest = self.advance_and_rest()
        self.inline = SalesInvoicesInline(ProjectFile, admin.site)

    def _listed(self):
        return {invoice.pk: invoice for invoice in self.inline.get_form_queryset(self.project_file)}

    def test_this_pf_column(self):
        listed = self._listed()

        self.assertEqual(set(listed), {self.advance.pk, self.rest.pk})
        self.assertEqual(self.inline.pf_amount(listed[self.advance.pk]).amount, Decimal("554.06"))
        self.assertEqual(self.inline.pf_amount(listed[self.rest.pk]).amount, Decimal("1292.81"))

    def test_type_badge(self):
        listed = self._listed()

        for invoice in (self.advance, self.rest):
            with self.subTest(document_type=invoice.document_type):
                self.assertIn(
                    str(invoice.get_document_type_display()),
                    str(self.inline.type_display(listed[invoice.pk])),
                )


class _ProjectFileAdminTestBase(AdvanceExpedienteMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.model_admin = ProjectFileAdmin(ProjectFile, admin.site)

    def refreshed(self):
        return ProjectFile.objects.get(pk=self.project_file.pk)

    @staticmethod
    def advance_text(percentage):
        return gettext("+%(percentage)s%% advance") % {"percentage": Decimal(percentage)}


class InvoicingColumnTests(_ProjectFileAdminTestBase):
    def _column(self):
        return str(self.model_admin.invoicing_status(self.refreshed()))

    def test_without_advances(self):
        self.invoice_goods(Decimal("0.5"))
        column = self._column()
        self.assertIn("50.00%", column)
        self.assertIsNone(ADVANCE_PART.search(column))

    def test_pending_advance_is_shown_apart(self):
        self.make_advance(Decimal("300.00"), "30")
        column = self._column()
        self.assertIn("0.00%", column)
        self.assertIn(self.advance_text("30.00"), column)

        # Half the goods, deducting half the advance: 50 % goods + 15 % advance
        self.rest_invoice(Decimal("0.5"))
        column = self._column()
        self.assertIn("50.00%", column)
        self.assertIn(self.advance_text("15.00"), column)

    def test_fully_deducted(self):
        self.make_advance(Decimal("300.00"), "30")
        self.rest_invoice()

        column = self._column()

        self.assertIn("✓ 100.00%", column)
        self.assertIsNone(ADVANCE_PART.search(column))

    def test_changelist_renders(self):
        self.make_advance(Decimal("300.00"), "30")
        self.client.force_login(User.objects.create_superuser("pf-admin", "p@x.com", "pw"))

        response = self.client.get(reverse("admin:management_projectfile_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.advance_text("30.00"))


class InvoicingSummaryTests(_ProjectFileAdminTestBase):
    def _summary(self):
        return str(self.model_admin.invoicing_summary_display(self.refreshed()))

    def test_no_advance_tiles_without_advances(self):
        self.invoice_goods(Decimal("0.5"))
        self.assertNotIn(gettext("Pending to deduct"), self._summary())

    def test_advance_tiles_and_rows(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.rest_invoice(Decimal("0.5"))

        html = self._summary()

        self.assertIn(gettext("Pending to deduct"), html)
        self.assertIn("300.00 €", html)   # advanced
        self.assertIn("150.00 €", html)   # deducted, and pending
        self.assertIn(advance.display_number, html)
        self.assertIn(reverse("admin:invoices_salesinvoice_change", args=[advance.pk]), html)
        self.assertIn(gettext("Unpaid"), html)
        self.assertNotIn(gettext("Unsettled advance: no goods are left to deduct it from"), html)

        SalesInvoice.objects.filter(pk=advance.pk).update(payment_status="paid")
        self.assertNotIn(gettext("Unpaid"), self._summary())

    def test_pro_forma_advance_is_flagged(self):
        self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

        html = self._summary()

        self.assertIn(str(SalesInvoiceStatus.PROFORMA.label), html)
        self.assertNotIn(gettext("Unpaid"), html)

    def test_cancelled_advances_are_left_out(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(advance, SalesInvoiceStatus.CANCELLED)

        self.assertNotIn(gettext("Pending to deduct"), self._summary())

    def test_unsettled_advance(self):
        # Every good invoiced but the advance left pending: nothing is left
        # to deduct it from (as when the expediente shrank)
        self.make_advance(Decimal("300.00"), "30")
        self.invoice_goods()

        self.assertIn(gettext("Unsettled advance: no goods are left to deduct it from"), self._summary())

    def test_change_page_renders(self):
        self.make_advance(Decimal("300.00"), "30")
        self.rest_invoice(Decimal("0.5"))
        self.client.force_login(User.objects.create_superuser("pf-admin", "p@x.com", "pw"))

        response = self.client.get(reverse("admin:management_projectfile_change", args=[self.project_file.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, gettext("Pending to deduct"))


class ClosingWithAdvancesPendingTests(_ProjectFileAdminTestBase):
    """As the API, the admin form refuses to complete or cancel an
    expediente whose advances are not deducted yet."""

    def _form(self, status):
        project_file = self.refreshed()
        return ProjectFileAdminForm(
            data={
                "name": project_file.name,
                "company": self.company.pk,
                "client": self.client_obj.pk,
                "status": status,
                "start_date": "2026-07-16",
                # Cached fields the plain form (fields="__all__") requires
                "total_amount_0": "0", "total_amount_1": "EUR",
                "total_invoiced_0": "0", "total_invoiced_1": "EUR",
                "total_purchased_0": "0", "total_purchased_1": "EUR",
                "total_costs_0": "0", "total_costs_1": "EUR",
                "percentage_invoiced": "0",
            },
            instance=project_file,
        )

    def test_refused_while_an_advance_is_pending(self):
        self.make_advance(Decimal("300.00"), "30")

        for status in (ProjectFileStatus.COMPLETED, ProjectFileStatus.CANCELLED):
            with self.subTest(status=status):
                form = self._form(status)
                self.assertFalse(form.is_valid())
                self.assertEqual(list(form.errors), ["status"])
        self.assertTrue(self._form(ProjectFileStatus.ACTIVE).is_valid())

    def test_allowed_once_deducted(self):
        self.make_advance(Decimal("300.00"), "30")
        self.rest_invoice()

        self.assertTrue(self._form(ProjectFileStatus.COMPLETED).is_valid())

    def test_an_expediente_already_closed_can_still_be_edited(self):
        self.make_advance(Decimal("300.00"), "30")
        ProjectFile.objects.filter(pk=self.project_file.pk).update(status=ProjectFileStatus.COMPLETED)

        self.assertTrue(self._form(ProjectFileStatus.COMPLETED).is_valid())
