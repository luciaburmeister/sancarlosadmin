"""
The sales invoice admin with advance invoices:

- the changelist filters by document type and labels each row with it;
- an advance shows where it has been deducted and what is left to deduct,
  with an "unpaid" badge while a final advance is not paid;
- an invoice that deducts advances lists them, flagging the unpaid ones;
- the expediente, company, client and currency are read-only on advances
  and on the invoices that deduct them, pro formas included.
"""
import re
from decimal import Decimal

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils.formats import number_format
from django.utils.html import strip_tags
from django.utils.translation import gettext

from invoices.admin import ADVANCE_LOCKED_FIELDS, SalesInvoiceAdmin
from invoices.models import (
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceStatus,
)
from invoices.services.advances import AdvanceDeductionService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin


def _rows(html):
    """The panel's table rows, as lists of cell texts."""
    return [
        [strip_tags(cell).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", str(html), re.S)
    ]


class _AdvanceAdminTestBase(AdvanceExpedienteMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser("advances-admin", "a@x.com", "pw")
        self.client.force_login(self.user)
        self.request = RequestFactory().get("/")
        self.request.user = self.user
        self.model_admin = SalesInvoiceAdmin(SalesInvoice, admin.site)

    def rest_invoice(self):
        """A pro forma billing all the expediente's goods, deducting its advances."""
        invoice = self.invoice_goods()
        AdvanceDeductionService.apply(invoice)
        return invoice

    def fresh(self, invoice):
        """The invoice as the admin loads it (nothing cached on the instance)."""
        return SalesInvoice.objects.get(pk=invoice.pk)

    def mark_paid(self, invoice):
        SalesInvoice.objects.filter(pk=invoice.pk).update(payment_status=InvoicePaymentStatus.PAID)


class ChangelistTests(_AdvanceAdminTestBase):
    def setUp(self):
        super().setUp()
        self.advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.rest = self.rest_invoice()

    def _listed(self, **params):
        response = self.client.get(reverse("admin:invoices_salesinvoice_changelist"), params)
        self.assertEqual(response.status_code, 200)
        return sorted(invoice.pk for invoice in response.context["cl"].result_list)

    def test_filter_by_document_type(self):
        self.assertEqual(self._listed(), sorted([self.advance.pk, self.rest.pk]))
        self.assertEqual(self._listed(document_type__exact="advance"), [self.advance.pk])
        self.assertEqual(self._listed(document_type__exact="invoice"), [self.rest.pk])

    def test_type_label(self):
        self.assertEqual(
            self.model_admin.get_document_type_label(self.advance)[0], SalesInvoiceDocumentType.ADVANCE,
        )
        self.assertEqual(
            self.model_admin.get_document_type_label(self.rest)[0], SalesInvoiceDocumentType.INVOICE,
        )


class AdvancePanelTests(_AdvanceAdminTestBase):
    def setUp(self):
        super().setUp()
        self.advance, _ = self.make_advance(Decimal("300.00"), "30")

    def _panel(self):
        return self.model_admin.advances_display(self.fresh(self.advance))

    def test_where_it_was_deducted_and_what_is_left(self):
        rest = self.rest_invoice()

        rows = _rows(self._panel())

        self.assertEqual(
            [row[1] for row in rows],
            [
                f"{number_format(Decimal('30.00'), 2)} %",
                number_format(Decimal("300.00"), 2),
                number_format(Decimal("-300.00"), 2),
                number_format(Decimal("0.00"), 2),
            ],
        )
        self.assertIn(rest.display_number, rows[2][0])
        # The deducting invoice is still a pro forma: it says so
        self.assertIn(rest.get_status_display(), rows[2][0])

    def test_deductions_on_cancelled_invoices_are_left_out(self):
        rest = self.rest_invoice()
        self.set_status(rest, SalesInvoiceStatus.CANCELLED)

        rows = _rows(self._panel())

        self.assertEqual(len(rows), 3)  # %, base, pending
        self.assertNotIn(rest.display_number, str(self._panel()))
        self.assertEqual(rows[-1][1], number_format(Decimal("300.00"), 2))

    def test_unpaid_badge_on_a_final_advance(self):
        self.assertIn(gettext("Unpaid"), _rows(self._panel())[0][2])

        self.mark_paid(self.advance)
        self.assertNotIn(gettext("Unpaid"), _rows(self._panel())[0][2])

    def test_pro_forma_advance_is_flagged_not_unpaid(self):
        proforma, _ = self.make_advance(Decimal("100.00"), "10", status=SalesInvoiceStatus.PROFORMA)

        badges = _rows(self.model_admin.advances_display(self.fresh(proforma)))[0][2]

        self.assertIn(gettext("pro forma: issue it as final to deduct it"), badges)
        self.assertNotIn(gettext("Unpaid"), badges)

    def test_change_page_shows_the_panel(self):
        response = self.client.get(reverse("admin:invoices_salesinvoice_change", args=[self.advance.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertIn("advances_display", response.context["adminform"].readonly_fields)
        self.assertIn(
            "advances_display",
            [field for _, options in response.context["adminform"].fieldsets for field in options["fields"]],
        )


class DeductionsPanelTests(_AdvanceAdminTestBase):
    def test_lists_the_advances_it_deducts(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        rest = self.rest_invoice()

        [row] = _rows(self.model_admin.advances_display(self.fresh(rest)))

        self.assertIn(advance.display_number, row[0])
        self.assertIn(advance.date_issued.strftime("%d/%m/%Y"), row[0])
        self.assertEqual(row[1], number_format(Decimal("-300.00"), 2))
        self.assertIn(gettext("Unpaid"), row[2])

        self.mark_paid(advance)
        [row] = _rows(self.model_admin.advances_display(self.fresh(rest)))
        self.assertEqual(row[2], "")

    def test_no_panel_without_advances(self):
        invoice = self.invoice_goods()

        fieldsets = self.model_admin.get_fieldsets(self.request, self.fresh(invoice))

        self.assertNotIn(
            "advances_display", [field for _, options in fieldsets for field in options["fields"]],
        )

    def test_change_page_renders(self):
        self.make_advance(Decimal("300.00"), "30")
        rest = self.rest_invoice()

        response = self.client.get(reverse("admin:invoices_salesinvoice_change", args=[rest.pk]))

        self.assertEqual(response.status_code, 200)


class LockedHeaderTests(_AdvanceAdminTestBase):
    def _readonly(self, invoice):
        return set(self.model_admin.get_readonly_fields(self.request, self.fresh(invoice)))

    def test_pro_forma_advance(self):
        proforma, _ = self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)
        self.assertTrue(set(ADVANCE_LOCKED_FIELDS) <= self._readonly(proforma))

    def test_final_advance_keeps_its_project_file(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        self.assertTrue(set(ADVANCE_LOCKED_FIELDS) <= self._readonly(advance))

    def test_pro_forma_that_deducts_an_advance(self):
        self.make_advance(Decimal("300.00"), "30")
        rest = self.rest_invoice()
        self.assertTrue(set(ADVANCE_LOCKED_FIELDS) <= self._readonly(rest))

    def test_other_invoices_are_unchanged(self):
        invoice = self.invoice_goods()
        self.assertFalse(set(ADVANCE_LOCKED_FIELDS) & self._readonly(invoice))

        # A final invoice's expediente can still be corrected
        self.set_status(invoice, SalesInvoiceStatus.FINAL)
        readonly = self._readonly(invoice)
        self.assertNotIn("project_file", readonly)
        self.assertTrue({"company", "client", "currency"} <= readonly)

    def test_change_form_has_no_header_fields(self):
        proforma, _ = self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

        response = self.client.get(reverse("admin:invoices_salesinvoice_change", args=[proforma.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(set(ADVANCE_LOCKED_FIELDS) & set(response.context["adminform"].form.fields))
