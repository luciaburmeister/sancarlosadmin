"""
Model guards and finalize rules the advance invoices build on:

- issued (FINAL) and CANCELLED sales invoices are frozen: no new lines, no
  line edits, no new or changed taxes, per-line taxes included;
- a deduction line (``advance_line`` set) never has a positive total, and an
  advance line that has been deducted cannot be deleted;
- the advance CheckConstraints;
- only a pro forma can be finalized, and every caller turns the refusal into
  a message or a 400 instead of a 500;
- a zero-total invoice is settled (PAID) as is;
- the manual wizard always creates a pro forma;
- every invoicing path locks its expedientes before the numbering rows;
- deleting or cancelling the invoice that deducts an advance releases it;
- finalize recalculates the totals of advances and of invoices deducting
  them (any other pro forma, a delivery orders' invoice included, is issued
  as sent) and refuses deductions that no longer hold; a manual credit note
  is issued as before;
- finalize, apply and remove decide on the invoice's row, not on a copy
  loaded before another request issued it; the AEAT submission and the
  client email are queued once the finalize commits;
- an advance still deducted or paid cannot be cancelled (admin action and
  task, which records the pending cancellation before calling the
  provider); without an e-invoicing account it is cancelled locally, and
  the admin says so;
- the admin's Finalize and Cancel at AEAT need the change permission, only
  reach the user's companies' invoices, and only run on their dialog's
  CSRF-protected POST (a plain link opens the dialog);
- advances and invoices deducting them cannot be duplicated;
- no path invoices goods while an advance is still a pro forma, nor
  invoices or issues them while an advance's cancellation at the AEAT is
  in flight (queued or sent, no answer yet).
"""
import json
import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib import admin, messages
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.translation import gettext
from djmoney.money import Money
from rest_framework.test import APIClient

from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from apps.einvoicing.providers.base import SubmissionResult
from apps.einvoicing.tasks import cancel_invoice_at_aeat
from company.models import Company, CompanyUser, Employee
from configurator.models import Configuration
from invoices.admin import AppliedTaxInline, SalesInvoiceItemInline
from invoices.models import (
    AppliedTax,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    TaxType,
)
from invoices.services.discounts import (
    is_advance_deduction_source,
    is_advance_line_source,
    is_discount_line_source,
    source_kind,
)
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.delivery_order_service import DeliveryOrderService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.advance_fixtures import (
    AdvanceExpedienteMixin,
    ExpedienteInvoicingMixin,
    UserExpedienteMixin,
    aeat_cancel_marker,
)
from invoices.tests.billing.test_project_file_invoice_pricing_golden import (
    _GoldenExpedienteBase,
    _RetentionExpedienteBase,
)
from management.models import ProjectFile
from materials.models import PaymentMethod
from payments.models import Payment, PaymentAllocation, PaymentDirection, PaymentStatus
from sales.models import Client


class _InvoiceFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Fistex")
        cls.client_obj = Client.objects.create(name="Cliente SA", company=cls.company)
        cls.project_file = ProjectFile.objects.create(
            name="Obra", company=cls.company, client=cls.client_obj
        )
        cls.iva, _ = TaxType.objects.get_or_create(
            tax_name="IVA", country="ES", defaults={"behavior": "add"}
        )

    def _invoice(self, status=SalesInvoiceStatus.PROFORMA, **kwargs):
        return SalesInvoice.objects.create(
            company=self.company, client=self.client_obj, status=status, **kwargs
        )

    def _line(self, invoice, unit_price="100.00", **kwargs):
        return SalesInvoiceItem.objects.create(
            invoice=invoice, description="Ventana", quantity=Decimal("1"),
            unit_price=Money(unit_price, invoice.currency), **kwargs,
        )

    def _tax(self, obj, pct="21", **kwargs):
        return AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(obj),
            object_id=obj.pk,
            tax_type=self.iva,
            selected_percentage=Decimal(pct),
            **kwargs,
        )

    def _set_status(self, invoice, status):
        invoice.status = status
        invoice.save(update_fields=["status"])


class SalesInvoiceItemGuardTests(_InvoiceFixtures):
    def test_lines_can_be_added_and_edited_on_a_proforma(self):
        invoice = self._invoice()
        line = self._line(invoice)
        line.unit_price = Money("150.00", "EUR")
        line.save()
        line.refresh_from_db()
        self.assertEqual(line.line_total.amount, Decimal("150.00"))

    def test_no_new_line_on_a_final_or_cancelled_invoice(self):
        for status in (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED):
            with self.subTest(status=status):
                invoice = self._invoice(status=status)
                with self.assertRaises(ValidationError):
                    self._line(invoice)
                self.assertFalse(invoice.items.exists())

    def test_no_line_edit_on_a_final_or_cancelled_invoice(self):
        for status in (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED):
            with self.subTest(status=status):
                invoice = self._invoice()
                line = self._line(invoice)
                self._set_status(invoice, status)
                line.refresh_from_db()
                line.unit_price = Money("1.00", "EUR")
                with self.assertRaises(ValidationError):
                    line.save()
                line.refresh_from_db()
                self.assertEqual(line.unit_price.amount, Decimal("100.00"))

    def test_deduction_line_cannot_have_a_positive_total(self):
        advance = self._invoice(
            document_type=SalesInvoiceDocumentType.ADVANCE,
            project_file=self.project_file,
            advance_percentage=Decimal("30"),
        )
        advance_line = self._line(advance, "300.00", source_id=f"advance_pf_{self.project_file.pk}")
        rest = self._invoice(project_file=self.project_file)

        with self.assertRaises(ValidationError):
            self._line(rest, "300.00", source_id=f"advance_ded_{advance_line.pk}", advance_line=advance_line)

        deduction = self._line(rest, "-300.00", source_id=f"advance_ded_{advance_line.pk}", advance_line=advance_line)
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))
        self.assertEqual(list(advance_line.deductions.all()), [deduction])
        # Zero is allowed (nothing left to deduct is not an error).
        self._line(rest, "0.00", advance_line=advance_line)

    def test_positive_lines_without_advance_line_are_untouched(self):
        invoice = self._invoice()
        line = self._line(invoice, "-50.00")
        self.assertEqual(line.line_total.amount, Decimal("-50.00"))

    def test_deducted_advance_line_and_its_invoice_cannot_be_deleted(self):
        advance = self._invoice(
            document_type=SalesInvoiceDocumentType.ADVANCE,
            project_file=self.project_file,
            advance_percentage=Decimal("30"),
        )
        advance_line = self._line(advance, "300.00")
        rest = self._invoice(project_file=self.project_file)
        self._line(rest, "-300.00", advance_line=advance_line)

        with self.assertRaises(ProtectedError):
            advance_line.delete()
        with self.assertRaises(ProtectedError):
            advance.delete()
        self.assertTrue(SalesInvoiceItem.objects.filter(pk=advance_line.pk).exists())

        # Once the deducting invoice is gone the advance is free again.
        rest.delete()
        advance.delete()
        self.assertFalse(SalesInvoiceItem.objects.filter(pk=advance_line.pk).exists())

    def test_per_line_taxes_are_deleted_with_their_line(self):
        invoice = self._invoice()
        line = self._line(invoice)
        tax = self._tax(line)
        self.assertEqual(list(line.applied_taxes.all()), [tax])

        line.delete()
        self.assertFalse(AppliedTax.objects.filter(pk=tax.pk).exists())


class AppliedTaxGuardTests(_InvoiceFixtures):
    def test_no_new_invoice_tax_on_a_cancelled_invoice(self):
        invoice = self._invoice(status=SalesInvoiceStatus.CANCELLED)
        with self.assertRaises(ValidationError):
            self._tax(invoice)

    def test_invoice_tax_percentage_cannot_change_on_a_cancelled_invoice(self):
        invoice = self._invoice()
        tax = self._tax(invoice)
        self._set_status(invoice, SalesInvoiceStatus.CANCELLED)

        # A fresh instance, as the admin inline loads it (the tax caches its invoice).
        tax.refresh_from_db()
        tax.selected_percentage = Decimal("10")
        with self.assertRaises(ValidationError):
            tax.save()
        with self.assertRaises(ValidationError):
            tax.save(update_fields=["selected_percentage"])

    def test_no_new_per_line_tax_on_a_final_or_cancelled_invoice(self):
        for status in (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED):
            with self.subTest(status=status):
                invoice = self._invoice()
                line = self._line(invoice)
                self._set_status(invoice, status)
                with self.assertRaises(ValidationError):
                    self._tax(line)
                self.assertFalse(line.applied_taxes.exists())

    def test_per_line_tax_percentage_cannot_change_once_final(self):
        invoice = self._invoice()
        line = self._line(invoice)
        tax = self._tax(line, base_amount=Money("100.00", "EUR"))
        self._set_status(invoice, SalesInvoiceStatus.FINAL)

        tax.refresh_from_db()
        tax.selected_percentage = Decimal("10")
        with self.assertRaises(ValidationError):
            tax.save()

        # Computed fields may still be written, as on invoice-level taxes.
        tax.refresh_from_db()
        tax.calculated_amount = Money("21.00", "EUR")
        tax.save(update_fields=["calculated_amount"])

    def test_per_line_tax_takes_the_invoice_currency(self):
        invoice = self._invoice(currency="USD")
        line = self._line(invoice)
        tax = self._tax(
            line, base_amount=Decimal("100.00"), calculated_amount=Decimal("21.00")
        )
        tax.refresh_from_db()
        self.assertEqual(str(tax.base_amount.currency), "USD")
        self.assertEqual(str(tax.calculated_amount.currency), "USD")

    def test_currency_change_relabels_per_line_taxes(self):
        invoice = self._invoice()
        line = self._line(invoice)
        line_tax = self._tax(line, base_amount=Money("100.00", "EUR"), calculated_amount=Money("21.00", "EUR"))
        invoice_tax = self._tax(invoice)
        other_invoice = self._invoice()
        other_line = self._line(other_invoice)
        other_tax = self._tax(other_line, base_amount=Money("100.00", "EUR"))

        invoice.currency = "USD"
        invoice.save()

        for tax in (line_tax, invoice_tax):
            tax.refresh_from_db()
            self.assertEqual(str(tax.base_amount.currency), "USD")
            self.assertEqual(str(tax.calculated_amount.currency), "USD")
        other_tax.refresh_from_db()
        self.assertEqual(str(other_tax.base_amount.currency), "EUR")


class CancelledInvoiceTotalsTests(_InvoiceFixtures):
    def test_recalculate_totals_leaves_a_cancelled_invoice_alone(self):
        invoice = self._invoice()
        line = self._line(invoice)
        self._tax(invoice)
        invoice.recalculate_totals()
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))

        self._set_status(invoice, SalesInvoiceStatus.CANCELLED)
        # Bypass the model guard, as a bulk update would.
        SalesInvoiceItem.objects.filter(pk=line.pk).update(line_total=Decimal("500.00"))

        invoice.recalculate_totals()
        invoice.save()
        invoice.refresh_from_db()
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("100.00"))
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))


class PaymentStatusTests(_InvoiceFixtures):
    def test_zero_total_invoice_is_paid(self):
        invoice = self._invoice(status=SalesInvoiceStatus.FINAL, total_amount=Money("0.00", "EUR"))
        invoice.recalculate_payment_status()
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

    def test_positive_total_without_payments_stays_unpaid(self):
        invoice = self._invoice(status=SalesInvoiceStatus.FINAL, total_amount=Money("10.00", "EUR"))
        invoice.recalculate_payment_status()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)

    def test_negative_total_credit_note_keeps_todays_behaviour(self):
        invoice = self._invoice(status=SalesInvoiceStatus.FINAL, total_amount=Money("-40.00", "EUR"))
        invoice.recalculate_payment_status()
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)


class AdvanceConstraintTests(_InvoiceFixtures):
    def _assert_refused(self, **kwargs):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._invoice(**kwargs)

    def test_advance_needs_a_project_file(self):
        self._assert_refused(document_type=SalesInvoiceDocumentType.ADVANCE)

    def test_advance_percentage_only_on_advances_and_within_range(self):
        self._assert_refused(advance_percentage=Decimal("30"))
        for pct in ("0", "-5", "100.0001"):
            with self.subTest(pct=pct):
                self._assert_refused(
                    document_type=SalesInvoiceDocumentType.ADVANCE,
                    project_file=self.project_file,
                    advance_percentage=Decimal(pct),
                )

    def test_valid_advance_and_default_invoice(self):
        advance = self._invoice(
            document_type=SalesInvoiceDocumentType.ADVANCE,
            project_file=self.project_file,
            advance_percentage=Decimal("100"),
        )
        self.assertEqual(advance.advance_percentage, Decimal("100"))
        # An advance may leave the % empty (amount mode derives it later).
        self._invoice(document_type=SalesInvoiceDocumentType.ADVANCE, project_file=self.project_file)

        invoice = self._invoice()
        self.assertEqual(invoice.document_type, SalesInvoiceDocumentType.INVOICE)
        self.assertIsNone(invoice.advance_percentage)


class AdvanceSourceIdTests(TestCase):
    def test_advance_prefixes(self):
        self.assertTrue(is_advance_line_source("advance_pf_12"))
        self.assertFalse(is_advance_line_source("advance_ded_12"))
        self.assertTrue(is_advance_deduction_source("advance_ded_7"))
        self.assertFalse(is_advance_deduction_source("advance_pf_7"))
        for empty in ("", None):
            self.assertFalse(is_advance_line_source(empty))
            self.assertFalse(is_advance_deduction_source(empty))

    def test_advance_lines_are_not_goods_nor_discounts(self):
        for source_id in ("advance_pf_12", "advance_ded_7"):
            with self.subTest(source_id=source_id):
                self.assertIsNone(source_kind(source_id))
                self.assertFalse(is_discount_line_source(source_id))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class FinalizeOnlyFromProformaTests(_InvoiceFixtures):
    def test_proforma_is_finalized(self):
        invoice = self._invoice()
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertIsNotNone(invoice.invoice_number)

    def test_final_invoice_is_returned_as_is(self):
        invoice = self._invoice()
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        number = invoice.invoice_number

        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.invoice_number, number)
        self.assertEqual(
            SalesInvoiceService.get_finalize_email_status(invoice)["email_skip_reason"],
            "already_final",
        )

    def test_cancelled_invoice_cannot_be_finalized_again(self):
        invoice = self._invoice()
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        number = invoice.invoice_number
        self._set_status(invoice, SalesInvoiceStatus.CANCELLED)

        with self.assertRaises(ValidationError):
            SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.CANCELLED)
        self.assertEqual(invoice.invoice_number, number)

    def test_negative_manual_invoice_still_finalizes(self):
        invoice = self._invoice()
        self._line(invoice, "-40.00")
        invoice.recalculate_totals()
        self.assertEqual(invoice.total_amount.amount, Decimal("-40.00"))

        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)


@override_settings(AEAT_SUBMISSIONS_ENABLED=True)
class FinalizeQueuesOnCommitTests(_InvoiceFixtures):
    def test_aeat_submission_and_email_wait_for_the_commit(self):
        """A worker picking them up before the commit would find the
        invoice still a pro forma (or not at all)."""
        self.client_obj.email = "cliente@example.com"
        self.client_obj.save(update_fields=["email"])
        invoice = self._invoice()

        with mock.patch("apps.einvoicing.tasks.submit_invoice_to_aeat.delay") as submit, \
                mock.patch("core.tasks.send_invoice_email_task.delay") as email:
            with self.captureOnCommitCallbacks() as callbacks:
                SalesInvoiceService.finalize_invoice(invoice)
            submit.assert_not_called()
            email.assert_not_called()
            for callback in callbacks:
                callback()

        submit.assert_called_once_with(invoice.pk)
        email.assert_called_once()
        self.assertTrue(SalesInvoiceService.get_finalize_email_status(invoice)["email_queued"])


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class StaleInvoiceTests(AdvanceExpedienteMixin, TestCase):
    """A request holding a copy of the invoice loaded before another one
    issued it (a double click, a retry) decides on its row, not its copy."""

    def test_a_second_finalize_returns_the_issued_invoice(self):
        invoice = self.invoice_goods()
        stale = SalesInvoice.objects.get(pk=invoice.pk)
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        SalesInvoiceService.finalize_invoice(stale, send_email=False)

        self.assertEqual(
            (stale.status, stale.invoice_number, stale.incremental_id),
            (SalesInvoiceStatus.FINAL, invoice.invoice_number, invoice.incremental_id),
        )
        self.assertEqual(
            SalesInvoiceService.get_finalize_email_status(stale)["email_skip_reason"], "already_final",
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.incremental_id, 1)
        self.assertEqual(
            SalesInvoice.objects.filter(company=self.company, incremental_id__isnull=False).count(), 1,
        )

    def test_its_advances_are_not_removed_once_issued(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.invoice_goods()
        AdvanceDeductionService.apply(invoice)
        stale = SalesInvoice.objects.get(pk=invoice.pk)
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        with self.assertRaises(ValidationError):
            AdvanceDeductionService.remove(stale)

        invoice.refresh_from_db()
        self.assertEqual(len(AdvanceDeductionService.deduction_lines(invoice)), 1)
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))

    def test_no_advance_is_deducted_once_issued(self):
        self.make_advance(Decimal("300.00"), "30")
        # A partial invoice may leave the advance for later
        invoice = self.invoice_goods(Decimal("0.5"))
        stale = SalesInvoice.objects.get(pk=invoice.pk)
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)

        with self.assertRaises(ValidationError):
            AdvanceDeductionService.apply(stale)

        self.assertEqual(AdvanceDeductionService.deduction_lines(invoice), [])
        invoice.refresh_from_db()
        self.assertEqual(invoice.total_amount.amount, Decimal("605.00"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class FinalizeCallersTests(_InvoiceFixtures):
    def setUp(self):
        self.admin_user = User.objects.create_superuser("finalizer", "f@x.com", "pw")
        self.client.force_login(self.admin_user)

    def _cancelled_invoice(self):
        invoice = self._invoice()
        self._set_status(invoice, SalesInvoiceStatus.CANCELLED)
        return invoice

    def _messages(self, response):
        return [(m.level, str(m)) for m in get_messages(response.wsgi_request)]

    def _finalize_in_admin(self, action, invoice):
        """The action's dialog, submitted (htmx posts it)."""
        return self.client.post(reverse(action, args=[invoice.pk]), {"_form_submitted": "True"})

    def _assert_admin_finalize_refused(self, action, target):
        invoice = self._cancelled_invoice()
        response = self._finalize_in_admin(action, invoice)
        self.assertEqual(response["HX-Redirect"], target(invoice))
        self.assertEqual([level for level, _ in self._messages(response)], [messages.ERROR])
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.CANCELLED)
        self.assertIsNone(invoice.invoice_number)

    def test_admin_row_finalize_of_a_cancelled_invoice_shows_a_message(self):
        self._assert_admin_finalize_refused(
            "admin:invoices_salesinvoice_finalize_invoice_row_action",
            lambda invoice: reverse("admin:invoices_salesinvoice_changelist"),
        )

    def test_admin_detail_finalize_of_a_cancelled_invoice_shows_a_message(self):
        self._assert_admin_finalize_refused(
            "admin:invoices_salesinvoice_finalize_invoice_detail_action",
            lambda invoice: reverse("admin:invoices_salesinvoice_change", args=[invoice.pk]),
        )

    def test_admin_bulk_finalize_reports_each_failure_and_keeps_going(self):
        good = self._invoice()
        bad = self._invoice()

        def fake_finalize(invoice, **kwargs):
            if invoice.pk == bad.pk:
                raise ValidationError("refused")
            return invoice

        with mock.patch(
            "invoices.admin.SalesInvoiceService.finalize_invoice", side_effect=fake_finalize
        ) as finalize:
            response = self.client.post(
                reverse("admin:invoices_salesinvoice_changelist"),
                {"action": "finalize_invoice_action", "_selected_action": [good.pk, bad.pk]},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(finalize.call_count, 2)
        found = self._messages(response)
        errors = [text for level, text in found if level == messages.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn(bad.display_number, errors[0])
        self.assertNotIn(good.display_number, errors[0])
        self.assertIn("1", [text for level, text in found if level == messages.INFO][0])

    def test_api_finalize_of_a_cancelled_invoice_is_a_400(self):
        user = User.objects.create_user("api-user", password="pw")
        Employee.objects.create(
            user=user, company=self.company, first_name="A", last_name="B",
            is_active=True, hired_at=date(2020, 1, 1),
        )
        api = APIClient()
        api.force_authenticate(user=user)
        invoice = self._cancelled_invoice()

        response = api.post(
            f"/es/api/sales-invoices/{invoice.pk}/finalize/",
            data=json.dumps({"send_email": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.CANCELLED)

    def test_admin_finalize_names_the_invoice(self):
        invoice = self._invoice()
        self._line(invoice)

        response = self._finalize_in_admin(
            "admin:invoices_salesinvoice_finalize_invoice_detail_action", invoice,
        )

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        # Finalizing generates no delivery order: the message says what it did
        self.assertEqual(
            self._messages(response),
            [(messages.INFO, gettext("Invoice %s finalized.") % invoice.invoice_number)],
        )


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class InvoiceActionAccessTests(_InvoiceFixtures):
    """Finalize and Cancel at AEAT need the change permission, only reach
    the user's own companies' invoices, and only run on their dialog's
    CSRF-protected POST."""

    ACTIONS = (
        "finalize_invoice_row_action", "finalize_invoice_detail_action",
        "cancel_aeat_row_action", "cancel_aeat_detail_action",
    )

    def setUp(self):
        self.proforma = self._invoice()
        self._line(self.proforma)
        self.final = self._invoice(status=SalesInvoiceStatus.FINAL, invoice_number="INV-2026-00001")

    def _staff(self, username, company, codename):
        user = User.objects.create_user(username, password="pw", is_staff=True)
        CompanyUser.objects.create(user=user, company=company, is_active=True)
        user.user_permissions.add(Permission.objects.get(codename=codename))
        self.client.force_login(user)
        return user

    def _url(self, name):
        invoice = self.proforma if name.startswith("finalize") else self.final
        return reverse(f"admin:invoices_salesinvoice_{name}", args=[invoice.pk])

    def _assert_unchanged(self):
        self.proforma.refresh_from_db()
        self.final.refresh_from_db()
        self.assertEqual(
            (self.proforma.status, self.proforma.invoice_number, self.final.status),
            (SalesInvoiceStatus.PROFORMA, None, SalesInvoiceStatus.FINAL),
        )
        self.assertFalse(EInvoicingSubmission.objects.exists())

    def _assert_status(self, code):
        for name in self.ACTIONS:
            with self.subTest(action=name), mock.patch("invoices.admin.cancel_invoice_at_aeat") as task:
                # The dialog, submitted (htmx posts it)
                response = self.client.post(self._url(name), {"_form_submitted": "True"})
                self.assertEqual(response.status_code, code)
                task.delay.assert_not_called()
        self._assert_unchanged()

    def test_a_view_only_user_is_refused(self):
        self._staff("viewer", self.company, "view_salesinvoice")
        self._assert_status(403)

    def test_another_companys_invoices_are_not_found(self):
        self._staff("other", Company.objects.create(name="Zeta Obras"), "change_salesinvoice")
        self._assert_status(404)

    def test_a_plain_link_only_opens_the_dialog(self):
        # A GET (a link, maybe from another site) changes nothing: neither
        # without an e-invoicing account, where Cancel at AEAT cancels the
        # invoice here, nor with one, where it queues the cancellation
        self._staff("editor", self.company, "change_salesinvoice")
        for has_account in (False, True):
            if has_account:
                EInvoicingAccount.objects.create(company=self.company, name="Verifacti")
            for name in self.ACTIONS:
                with (
                    self.subTest(action=name, has_account=has_account),
                    mock.patch("invoices.admin.cancel_invoice_at_aeat") as task,
                ):
                    response = self.client.get(self._url(name))
                    self.assertContains(response, 'name="_form_submitted"')
                    task.delay.assert_not_called()
        self._assert_unchanged()

    def test_cancel_runs_on_the_dialogs_post_with_its_csrf_token(self):
        # Without an e-invoicing account: cancelled here, right away
        csrf_client = self.client_class(enforce_csrf_checks=True)
        csrf_client.force_login(self._staff("editor", self.company, "change_salesinvoice"))
        url = self._url("cancel_aeat_detail_action")

        # Posted without the dialog's token (e.g. from another site)
        response = csrf_client.post(url, {"_form_submitted": "True"})
        self.assertEqual(response.status_code, 403)
        self._assert_unchanged()

        dialog = csrf_client.get(url).content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', dialog).group(1)
        response = csrf_client.post(url, {"_form_submitted": "True", "csrfmiddlewaretoken": token})

        self.assertEqual(
            response["HX-Redirect"], reverse("admin:invoices_salesinvoice_change", args=[self.final.pk]),
        )
        self.final.refresh_from_db()
        self.assertEqual(self.final.status, SalesInvoiceStatus.CANCELLED)


class FrozenInlineTests(_InvoiceFixtures):
    def test_item_and_tax_inlines_are_read_only_on_final_and_cancelled(self):
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser("inline-admin", "i@x.com", "pw")
        item_inline = SalesInvoiceItemInline(SalesInvoice, admin.site)
        tax_inline = AppliedTaxInline(SalesInvoice, admin.site)

        proforma = self._invoice()
        for inline in (item_inline, tax_inline):
            self.assertTrue(inline.has_add_permission(request, proforma))
            self.assertTrue(inline.has_change_permission(request, proforma))
            self.assertTrue(inline.has_delete_permission(request, proforma))

        for status in (SalesInvoiceStatus.FINAL, SalesInvoiceStatus.CANCELLED):
            invoice = self._invoice(status=status)
            for inline in (item_inline, tax_inline):
                with self.subTest(status=status, inline=type(inline).__name__):
                    self.assertFalse(inline.has_add_permission(request, invoice))
                    self.assertFalse(inline.has_change_permission(request, invoice))
                    self.assertFalse(inline.has_delete_permission(request, invoice))


class ManualWizardTests(_InvoiceFixtures):
    STEP = "sales_invoice_wizard_view-current_step"

    def test_manual_wizard_always_creates_a_proforma(self):
        self.client.force_login(User.objects.create_superuser("wizard", "w@x.com", "pw"))
        url = reverse("admin:invoices_salesinvoice_wizard")

        response = self.client.post(url, {
            self.STEP: "basic",
            "basic-company": self.company.pk,
            # Not a field of the form any more: ignored.
            "basic-status": SalesInvoiceStatus.FINAL,
            "basic-client": self.client_obj.pk,
            "basic-date_issued": "2026-09-29",
            "basic-due_date": "2026-10-29",
            "basic-payment_method": PaymentMethod.TRANSFER,
            "basic-currency": "EUR",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard"]["steps"].current, "items")

        response = self.client.post(url, {
            self.STEP: "items",
            "items-TOTAL_FORMS": "1", "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0", "items-MAX_NUM_FORMS": "1000",
            "items-0-description": "Ventana corredera",
            "items-0-quantity": "1",
            "items-0-unit_price": "100.00",
            "items-0-discount_percentage": "0",
        })
        self.assertEqual(response.context["wizard"]["steps"].current, "taxes")

        response = self.client.post(url, {
            self.STEP: "taxes",
            "taxes-TOTAL_FORMS": "1", "taxes-INITIAL_FORMS": "0",
            "taxes-MIN_NUM_FORMS": "0", "taxes-MAX_NUM_FORMS": "1000",
            "taxes-0-tax_type": self.iva.pk,
            "taxes-0-selected_percentage": "21",
        })
        self.assertEqual(response.context["wizard"]["steps"].current, "notes")

        response = self.client.post(url, {self.STEP: "notes", "notes-notes": ""})

        invoice = SalesInvoice.objects.get(client=self.client_obj)
        self.assertRedirects(
            response, reverse("admin:invoices_salesinvoice_change", args=[invoice.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertIsNone(invoice.invoice_number)
        self.assertIsNotNone(invoice.proforma_number)
        self.assertEqual(invoice.items.count(), 1)
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class LockOrderTests(AdvanceExpedienteMixin, TestCase):
    """Every path that writes an expediente's invoices row-locks the
    expedientes first, before the proforma/invoice numbering row."""

    @staticmethod
    def _locked_tables(ctx):
        """The table of every SELECT ... FOR UPDATE, in order."""
        return [
            re.search(r'FROM "(\w+)"', query["sql"]).group(1)
            for query in ctx.captured_queries
            if "FOR UPDATE" in query["sql"]
        ]

    def _assert_project_file_locked_first(self, run):
        with CaptureQueriesContext(connection) as ctx:
            run()
        tables = self._locked_tables(ctx)
        self.assertEqual(tables[0], "management_projectfile")
        self.assertIn("invoices_salesinvoice", tables[1:])  # the numbering row

    def test_expediente_invoice(self):
        self._assert_project_file_locked_first(self.invoice_goods)

    def test_finalize(self):
        invoice = self.invoice_goods()
        self._assert_project_file_locked_first(
            lambda: SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        )

    def test_bulk_invoice(self):
        self._assert_project_file_locked_first(
            lambda: BulkProjectFileInvoiceService.create_bulk_invoice(
                [self.project_file], company=self.company, client=self.client_obj,
                date_issued=date(2026, 9, 29),
            )
        )

    def test_admin_change_of_an_invoice(self):
        """Saving it updates its expediente: the admin locks the expediente
        before the invoice row, as the invoicing paths do."""
        invoice = self.invoice_goods()
        request = RequestFactory().post("/")
        request.user = User.objects.create_superuser("lock-admin", "l@x.com", "pw")

        with CaptureQueriesContext(connection) as ctx:
            admin.site._registry[SalesInvoice].save_model(request, invoice, None, True)

        self.assertEqual(self._locked_tables(ctx)[:1], ["management_projectfile"])

    def test_delivery_order_invoice(self):
        delivery_order = self.make_delivery_order(config=self.config)
        self._assert_project_file_locked_first(
            lambda: self.invoice_delivery_orders(delivery_order)
        )


class ReleasingAdvancesTests(AdvanceExpedienteMixin, TestCase):
    """Deleting or cancelling the invoice that deducts an advance makes the
    advance pending again on the expediente."""

    def _rest_deducting_the_advance(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods()
        self.deduct(rest, line, Decimal("300.00"))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("100.00"))
        return rest

    def _assert_advance_pending_again(self):
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("300.00"))
        self.assertEqual(self.project_file.percentage_invoiced, Decimal("30.00"))

    def test_deleting_the_rest_proforma(self):
        self._rest_deducting_the_advance().delete()
        self._assert_advance_pending_again()

    def test_status_is_recomputed_once_the_deletion_commits(self):
        invoice = self.invoice_goods()
        with mock.patch("management.signals._recompute_project_file_status") as recompute:
            with self.captureOnCommitCallbacks() as callbacks:
                invoice.delete()
            recompute.assert_not_called()
            for callback in callbacks:
                callback()
        recompute.assert_called_once_with(self.project_file.pk)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class FinalizeDeductionsTests(AdvanceExpedienteMixin, TestCase):
    """validate_for_finalize on the goods invoices of an expediente of
    1,000.00 at IVA 21 % with advances."""

    def rest(self, ratio=Decimal("1"), amounts=None):
        """A pro forma of ``ratio`` of the goods deducting the default plan (or ``amounts``)."""
        invoice = self.invoice_goods(ratio)
        AdvanceDeductionService.apply(invoice, amounts)
        return invoice

    def finalize(self, invoice):
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        return invoice

    def assert_refused(self, invoice):
        with self.assertRaises(ValidationError):
            SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertIsNone(invoice.invoice_number)

    def test_the_rest_invoice_is_issued_with_its_deductions(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.finalize(self.rest())
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)

    def test_a_zero_total_is_paid_once_issued(self):
        self.make_advance(Decimal("1000.00"), "100")
        invoice = self.finalize(self.rest())
        self.assertEqual(invoice.total_amount.amount, Decimal("0.00"))
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.PAID)

    def test_totals_are_recalculated_before_it_is_issued(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.rest()
        SalesInvoice.objects.filter(pk=invoice.pk).update(total_amount=Decimal("0.00"))
        invoice.refresh_from_db()

        invoice = self.finalize(invoice)
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))
        self.assertEqual(invoice.payment_status, InvoicePaymentStatus.UNPAID)

    def test_the_expediente_is_updated_once(self):
        """Its totals go in the same save as its number: the expediente is
        recalculated once, not once per save."""
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.rest()
        recalculate = ProjectFile.recalculate_total_amount

        with mock.patch.object(
            ProjectFile, "recalculate_total_amount", autospec=True, side_effect=recalculate,
        ) as recalculated:
            invoice = self.finalize(invoice)

        self.assertEqual(recalculated.call_count, 1)
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))

    def _assert_refused_once_the_advance(self, change):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        invoice = self.rest()
        change(advance)
        self.assert_refused(invoice)

    def test_a_cancelled_advance_is_refused(self):
        self._assert_refused_once_the_advance(
            lambda advance: self.set_status(advance, SalesInvoiceStatus.CANCELLED)
        )

    def test_an_advance_being_cancelled_at_the_aeat_is_refused(self):
        self._assert_refused_once_the_advance(aeat_cancel_marker)

    def test_an_advance_of_another_client_is_refused(self):
        other = Client.objects.create(name="Otro", company=self.company)
        self._assert_refused_once_the_advance(
            lambda advance: SalesInvoice.objects.filter(pk=advance.pk).update(client=other)
        )

    def test_an_advance_deducted_twice_is_refused(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        first = self.invoice_goods(Decimal("0.5"))
        second = self.invoice_goods(Decimal("0.5"))
        # Bypassing the plan: each takes the whole advance
        for invoice in (first, second):
            self.deduct(invoice, line, Decimal("300.00"))

        for invoice in (first, second):
            self.assert_refused(invoice)

        AdvanceDeductionService.remove(second)
        self.finalize(first)

    def test_a_closing_invoice_must_deduct_what_is_pending(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.rest()
        AdvanceDeductionService.remove(invoice)
        self.assert_refused(invoice)

        AdvanceDeductionService.apply(invoice)
        self.finalize(invoice)

    def test_a_partial_invoice_may_leave_the_advance_pending(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.finalize(self.rest(Decimal("0.5"), amounts={}))
        self.assertEqual(invoice.total_amount.amount, Decimal("605.00"))

    def test_a_tax_base_its_deductions_did_not_lower_is_refused(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.rest()
        AppliedTax.objects.filter(
            content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=invoice.pk,
        ).update(base_amount=Decimal("1000.00"))
        self.assert_refused(invoice)

    def test_goods_added_after_the_deductions_are_refused(self):
        # Tax bases do not follow new lines: this one would go out untaxed
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.rest(Decimal("0.5"))
        self._line(invoice, "100.00", source_id=f"config_{self.config.pk}")
        self.assert_refused(invoice)

    def test_totals_below_zero_are_refused(self):
        self.make_advance(Decimal("1000.00"), "100")
        invoice = self.rest()
        self._line(invoice, "-10.00")
        self.assert_refused(invoice)

    def test_a_credit_note_of_the_expediente_is_issued_as_before(self):
        self.make_advance(Decimal("300.00"), "30")
        credit_note = SalesInvoice.objects.create(
            company=self.company, client=self.client_obj, project_file=self.project_file,
        )
        self._line(credit_note, "-40.00")
        credit_note.recalculate_totals()

        credit_note = self.finalize(credit_note)
        self.assertEqual(credit_note.total_amount.amount, Decimal("-40.00"))

    def test_a_cancelled_rest_invoice_cannot_be_issued_again(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        cancelled = self.finalize(self.rest())
        self.set_status(cancelled, SalesInvoiceStatus.CANCELLED)
        # The advance is free again: the replacement deducts it
        replacement = self.rest()
        self.assertEqual(
            [deduction.advance_line for deduction in AdvanceDeductionService.deduction_lines(replacement)],
            [line],
        )
        self.finalize(replacement)

        with self.assertRaises(ValidationError):
            SalesInvoiceService.finalize_invoice(cancelled, send_email=False)
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.status, SalesInvoiceStatus.CANCELLED)

    def _line(self, invoice, unit_price, source_id=""):
        SalesInvoiceItem.objects.create(
            invoice=invoice, description="Ajuste", quantity=Decimal("1"),
            unit_price=Money(unit_price, "EUR"), source_id=source_id,
        )


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class CancelAdvanceTests(AdvanceExpedienteMixin, TestCase):
    """Cancelling a final advance of 300.00 (IVA 21 %: 363.00)."""

    ROW = "admin:invoices_salesinvoice_cancel_aeat_row_action"
    DETAIL = "admin:invoices_salesinvoice_cancel_aeat_detail_action"

    def setUp(self):
        super().setUp()
        self.advance, self.line = self.make_advance(Decimal("300.00"), "30")
        self.client.force_login(User.objects.create_superuser("canceller", "c@x.com", "pw"))

    def deduct_on_the_rest(self):
        return self.rest_invoice()

    def pay(self):
        payment = Payment.objects.create(
            company=self.company, amount=Money("363.00", "EUR"), payment_date=date(2026, 9, 2),
            direction=PaymentDirection.INBOUND, method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED,
        )
        PaymentAllocation.objects.create(
            payment=payment, related_object=self.advance, amount_applied=Money("363.00", "EUR"),
        )

    def account(self):
        return EInvoicingAccount.objects.create(company=self.company, name="Verifacti")

    def post_dialog(self, action):
        """The action's dialog, submitted (htmx posts it)."""
        return self.client.post(reverse(action, args=[self.advance.pk]), {"_form_submitted": "True"})

    def cancel_in_admin(self, action):
        with mock.patch("invoices.admin.cancel_invoice_at_aeat") as task:
            response = self.post_dialog(action)
        return [m.level for m in get_messages(response.wsgi_request)], task

    def assert_still_final(self):
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.FINAL)
        self.assertFalse(
            EInvoicingSubmission.objects.filter(action=SubmissionAction.CANCEL).exists()
        )

    def test_the_admin_refuses_a_deducted_advance(self):
        self.deduct_on_the_rest()
        for action in (self.ROW, self.DETAIL):
            with self.subTest(action=action):
                levels, task = self.cancel_in_admin(action)
                # (the redirect is not followed: earlier messages are still there)
                self.assertEqual(set(levels), {messages.ERROR})
                task.delay.assert_not_called()

    def test_the_admin_refuses_a_paid_advance(self):
        self.pay()
        levels, task = self.cancel_in_admin(self.DETAIL)
        self.assertEqual(levels, [messages.ERROR])
        task.delay.assert_not_called()

    def test_the_admin_queues_an_advance_nobody_deducts(self):
        self.account()
        levels, task = self.cancel_in_admin(self.ROW)
        self.assertEqual(levels, [messages.INFO])
        task.delay.assert_called_once_with(self.advance.pk)

    def test_without_an_einvoicing_account_the_admin_cancels_it_locally(self):
        with mock.patch("invoices.admin.cancel_invoice_at_aeat") as task:
            response = self.post_dialog(self.DETAIL)

        task.delay.assert_not_called()
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.CANCELLED)
        # Says so, instead of "queued"
        self.assertEqual(
            [(m.level, str(m)) for m in get_messages(response.wsgi_request)],
            [(
                messages.INFO,
                gettext("Invoice %s cancelled locally: its company has no e-invoicing account.")
                % (self.advance.invoice_number or self.advance.pk),
            )],
        )

    def test_the_task_refuses_a_deducted_advance_before_calling_the_provider(self):
        self.account()
        self.deduct_on_the_rest()
        with mock.patch("apps.einvoicing.services.get_provider") as get_provider:
            result = cancel_invoice_at_aeat(self.advance.pk)

        self.assertEqual(result["status"], "refused")
        get_provider.assert_not_called()
        self.assert_still_final()

    def test_the_task_refuses_a_paid_advance(self):
        self.account()
        self.pay()
        with mock.patch("apps.einvoicing.services.get_provider") as get_provider:
            result = cancel_invoice_at_aeat(self.advance.pk)

        self.assertEqual(result["status"], "refused")
        get_provider.assert_not_called()
        self.assert_still_final()

    def test_the_pending_cancellation_is_recorded_before_the_provider_is_called(self):
        self.account()
        seen = {}

        def cancel_at_the_provider(account, invoice):
            seen["markers"] = list(
                EInvoicingSubmission.objects.filter(
                    sales_invoice=invoice, action=SubmissionAction.CANCEL,
                ).values_list("status", flat=True)
            )
            seen["deductible"] = self.summary()["advances"][0]["deductible"]
            return SubmissionResult(success=True, status=SubmissionStatus.ACCEPTED)

        with mock.patch("apps.einvoicing.services.get_provider") as get_provider:
            get_provider.return_value.cancel_invoice.side_effect = cancel_at_the_provider
            result = cancel_invoice_at_aeat(self.advance.pk)

        self.assertEqual(seen, {"markers": [SubmissionStatus.PENDING], "deductible": False})
        self.assertEqual(result["status"], SubmissionStatus.ACCEPTED)
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.CANCELLED)

    def test_without_an_einvoicing_account_it_is_cancelled_locally(self):
        with self.assertLogs("apps.einvoicing.services", "INFO") as logs:
            result = cancel_invoice_at_aeat(self.advance.pk)

        self.assertEqual(result["status"], "cancelled")
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.status, SalesInvoiceStatus.CANCELLED)
        self.assertFalse(EInvoicingSubmission.objects.exists())
        self.assertIn(f"SalesInvoice {self.advance.pk} cancelled locally", "\n".join(logs.output))
        self.project_file.refresh_from_db()
        self.assertEqual(self.project_file.advance_pending_amount.amount, Decimal("0.00"))

    def test_a_pending_cancellation_takes_the_advance_off_the_deductions(self):
        # A pro forma from before the cancellation was asked for (no new
        # one is invoiced meanwhile)
        invoice = self.invoice_goods()
        aeat_cancel_marker(self.advance)

        self.assertEqual(AdvanceDeductionService.default_plan(invoice), [])
        self.assertEqual(AdvanceDeductionService.apply(invoice), [])


class DuplicateInvoiceTests(AdvanceExpedienteMixin, TestCase):
    def test_advances_and_invoices_deducting_them_are_not_duplicated(self):
        advance, _ = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods()
        AdvanceDeductionService.apply(rest)

        for invoice in (advance, rest):
            with self.subTest(invoice=invoice.display_number), self.assertRaises(ValidationError):
                SalesInvoiceService.duplicate_invoice(invoice)
        self.assertEqual(SalesInvoice.objects.count(), 2)

    def test_other_invoices_still_are(self):
        invoice = self.invoice_goods()
        copy = SalesInvoiceService.duplicate_invoice(invoice)
        self.assertEqual(copy.items.count(), invoice.items.count())


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class ProformaAdvanceBlocksGoodsTests(AdvanceExpedienteMixin, TestCase):
    """While an advance is a pro forma no path invoices the expediente's
    goods: paying it, invoicing the goods in full and then finalizing it
    would bill 130 %."""

    def setUp(self):
        super().setUp()
        self.advance, _ = self.make_advance(
            Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA,
        )

    def assert_refused(self, create):
        with self.assertRaises(ValidationError) as ctx:
            create()
        self.assertIn(self.advance.display_number, " ".join(ctx.exception.messages))
        self.assertFalse(
            SalesInvoice.objects.filter(document_type=SalesInvoiceDocumentType.INVOICE).exists()
        )

    def test_expediente_invoice(self):
        self.assert_refused(self.invoice_goods)

    def test_bulk_invoice(self):
        self.assert_refused(lambda: BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        ))

    def test_delivery_order_invoice(self):
        delivery_order = self.make_delivery_order(config=self.config)
        self.assert_refused(lambda: self.invoice_delivery_orders(delivery_order))
        delivery_order.refresh_from_db()
        self.assertIsNone(delivery_order.sales_invoice_id)

    def test_issuing_the_advance_lets_the_goods_be_invoiced(self):
        SalesInvoiceService.finalize_invoice(self.advance, send_email=False)
        invoice = self.invoice_goods()

        [deduction] = AdvanceDeductionService.apply(invoice)
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))

    def test_the_wizard_refuses_it_on_the_setup_step(self):
        url = self.login_to_wizard()
        data = self.wizard_setup_data()
        self.client.get(url)

        response = self.client.post(url, data)
        self.assertEqual(response.context["wizard"]["steps"].current, "setup")
        self.assertIn("invoice_mode", response.context["wizard"]["form"].errors)

        # Another advance can still be issued
        response = self.client.post(url, {**data, "setup-invoice_mode": "advance", "setup-percentage": "10"})
        self.assertEqual(response.context["wizard"]["steps"].current, "notes")


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class CancellingAdvanceBlocksGoodsTests(AdvanceExpedienteMixin, TestCase):
    """While the AEAT has not answered the cancellation of a final advance
    of 300.00, no path invoices or issues the expediente's goods: the
    advance cannot be deducted meanwhile, and a rejected or failed
    cancellation leaves it to deduct again, on goods billed in full."""

    IN_FLIGHT = (SubmissionStatus.PENDING, SubmissionStatus.SUBMITTED)

    def setUp(self):
        super().setUp()
        self.advance, self.line = self.make_advance(Decimal("300.00"), "30")

    def cancel_marker(self, status):
        return aeat_cancel_marker(self.advance, status=status)

    def assert_refused_while_in_flight(self, create):
        for status in self.IN_FLIGHT:
            with self.subTest(status=status):
                marker = self.cancel_marker(status)
                with self.assertRaises(ValidationError) as ctx:
                    create()
                self.assertIn(self.advance.display_number, " ".join(ctx.exception.messages))
                self.assertFalse(
                    SalesInvoice.objects.filter(
                        document_type=SalesInvoiceDocumentType.INVOICE,
                    ).exists()
                )
                marker.delete()

    def test_expediente_invoice(self):
        self.assert_refused_while_in_flight(self.invoice_goods)

    def test_bulk_invoice(self):
        self.assert_refused_while_in_flight(lambda: BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        ))

    def test_delivery_order_invoice(self):
        delivery_order = self.make_delivery_order(config=self.config)
        self.assert_refused_while_in_flight(
            lambda: self.invoice_delivery_orders(delivery_order)
        )
        delivery_order.refresh_from_db()
        self.assertIsNone(delivery_order.sales_invoice_id)

    def test_the_wizard_refuses_it_on_the_setup_step(self):
        url = self.login_to_wizard()
        data = self.wizard_setup_data()
        for status in self.IN_FLIGHT:
            with self.subTest(status=status):
                marker = self.cancel_marker(status)
                self.client.get(url)

                response = self.client.post(url, data)
                self.assertEqual(response.context["wizard"]["steps"].current, "setup")
                self.assertIn(
                    self.advance.display_number,
                    " ".join(response.context["wizard"]["form"].errors["invoice_mode"]),
                )
                marker.delete()

    def test_a_goods_invoice_is_not_issued(self):
        # A pro forma deducting nothing, from before the cancellation was asked for
        invoice = self.invoice_goods()
        for status in self.IN_FLIGHT:
            with self.subTest(status=status):
                marker = self.cancel_marker(status)
                with self.assertRaises(ValidationError) as ctx:
                    SalesInvoiceService.finalize_invoice(invoice, send_email=False)
                self.assertIn(self.advance.display_number, " ".join(ctx.exception.messages))
                invoice.refresh_from_db()
                self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
                marker.delete()

    def assert_deducted_once_answered(self, status):
        marker = self.cancel_marker(SubmissionStatus.PENDING)
        with self.assertRaises(ValidationError):
            self.invoice_goods()
        marker.status = status
        marker.save(update_fields=["status"])

        invoice = self.invoice_goods()
        [deduction] = AdvanceDeductionService.apply(invoice)
        self.assertEqual(deduction.advance_line, self.line)
        self.assertEqual(deduction.line_total.amount, Decimal("-300.00"))
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(invoice.total_amount.amount, Decimal("847.00"))

    def test_a_rejected_cancellation_leaves_the_advance_to_deduct(self):
        self.assert_deducted_once_answered(SubmissionStatus.REJECTED)

    def test_a_failed_cancellation_leaves_the_advance_to_deduct(self):
        self.assert_deducted_once_answered(SubmissionStatus.ERROR)

    def test_a_cancelled_advance_is_not_deducted(self):
        marker = self.cancel_marker(SubmissionStatus.PENDING)
        marker.status = SubmissionStatus.ACCEPTED
        marker.save(update_fields=["status"])
        self.set_status(self.advance, SalesInvoiceStatus.CANCELLED)

        invoice = self.invoice_goods()
        self.assertEqual(AdvanceDeductionService.apply(invoice), [])
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(invoice.total_amount.amount, Decimal("1210.00"))


class _IssueAsBuiltMixin(ExpedienteInvoicingMixin):
    """A 30 % advance issued on the expediente, and issuing what the
    builders lay out: the finalize checks must refuse none of it and move
    no figure."""

    def setUp(self):
        super().setUp()
        self.advance = SalesInvoiceService.finalize_invoice(
            AdvanceInvoiceService.create(
                self.project_file, date_issued=date(2026, 9, 3), percentage=Decimal("30"),
            ),
            send_email=False,
        )

    def rest(self):
        invoice = self.invoice_goods()
        self.assertTrue(AdvanceDeductionService.apply(invoice))
        return invoice

    def issue(self, invoice):
        invoice.refresh_from_db()
        built = (invoice.subtotal_amount.amount, invoice.tax_amount.amount, invoice.total_amount.amount)
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(
            (invoice.subtotal_amount.amount, invoice.tax_amount.amount, invoice.total_amount.amount),
            built,
        )
        return invoice


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class IssueRetentionInvoicesTests(_IssueAsBuiltMixin, _RetentionExpedienteBase):
    """A 2 % discount, IVA 21 % and IRPF 15 %."""

    def test_rest_invoice(self):
        self.assertEqual(self.issue(self.rest()).total_amount.amount, Decimal("7271.60"))

    def test_delivery_order_invoices(self):
        Configuration.objects.filter(pk=self.config.pk).update(units=Decimal("2"))
        for _unit in range(2):
            [item] = DeliveryOrderService.get_deliverable_items(self.project_file)
            delivery_order = DeliveryOrderService.create_delivery_order(
                project_file=self.project_file, company=self.company, client=self.client_obj,
                items_data=[{**item, "quantity": Decimal("1")}],
            )
            invoice = self.invoice_delivery_orders(delivery_order)
            self.assertEqual(self.issue(invoice).total_amount.amount, Decimal("3635.80"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class IssueMultiRateInvoiceTests(_IssueAsBuiltMixin, _GoldenExpedienteBase):
    """IVA 21 % and 10 %, an untaxed line, a basket and a financing discount."""

    def test_rest_invoice(self):
        self.issue(self.rest())


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class IssueUserExpedienteInvoiceTests(_IssueAsBuiltMixin, UserExpedienteMixin, TestCase):
    """1,557.49 of line items with a 2 % financing rule, IVA 21 %."""

    def test_rest_invoice(self):
        self.assertEqual(self.issue(self.rest()).total_amount.amount, Decimal("1292.81"))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class IssuedAsSentTests(UserExpedienteMixin, TestCase):
    """1,557.49 of line items with a 2 % financing rule, IVA 21 %, and no
    advance: the pro forma sent to the client is what gets issued."""

    def _issue(self, invoice):
        invoice.refresh_from_db()
        sent = (invoice.subtotal_amount.amount, invoice.tax_amount.amount, invoice.total_amount.amount)
        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(
            (invoice.subtotal_amount.amount, invoice.tax_amount.amount, invoice.total_amount.amount),
            sent,
        )
        return invoice

    def test_expediente_invoice(self):
        invoice = self.invoice_goods()

        # Taxed per rate, as the quote: 1,526.34 × 21 % = 320.53
        self.assertEqual(
            (invoice.tax_amount.amount, invoice.total_amount.amount),
            (Decimal("320.53"), Decimal("1846.87")),
        )
        self._issue(invoice)

    def test_bulk_invoice(self):
        invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )

        self.assertEqual(
            (invoice.tax_amount.amount, invoice.total_amount.amount),
            (Decimal("320.53"), Decimal("1846.87")),
        )
        self._issue(invoice)

    def test_invoice_of_several_delivery_orders(self):
        """Each order rounds its own taxes (213.52 + 107.02): the invoice
        keeps their sum, as the orders show it, once issued too."""
        orders = [
            DeliveryOrderService.create_delivery_order(
                project_file=self.project_file, company=self.company, client=self.client_obj,
                items_data=[{**item, "quantity": item["remaining_quantity"]}],
            )
            for item in DeliveryOrderService.get_deliverable_items(self.project_file)
        ]
        invoice = self.invoice_delivery_orders(*orders)

        invoice = self._issue(invoice)
        self.assertEqual(
            (invoice.tax_amount.amount, invoice.total_amount.amount),
            (
                sum(order.tax_amount.amount for order in orders),
                sum(order.total_amount.amount for order in orders),
            ),
        )
