"""
The expediente invoicing summary with advance invoices, and the net
over-invoicing check (``AdvanceDeductionService.check``):

- an advance is money invoiced but not goods: the ratio base and the gates
  (``goods_remaining_net``, ``can_invoice``) leave it out and add its
  deductions back, so "100 % of the pending amount" still bills full lines;
- cancelled invoices bill nothing;
- per advance: base, deducted (pro formas included), pending, whether it
  can be deducted (FINAL, no cancellation at the AEAT) and whether its
  cancellation awaits the AEAT's answer;
- the goods still to invoice per VAT group, on request;
- the net check lets goods through after a 100 % advance or a cancelled
  invoice and refuses goods beyond the expediente's base.
"""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionAction,
    SubmissionStatus,
)
from invoices.models import InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus
from invoices.services.advances import AdvanceDeductionService
from invoices.services.bulk_invoice_service import BulkProjectFileInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin
from invoices.tests.test_project_file_invoice_pricing_golden import _GoldenExpedienteBase


class AdvanceSplitTests(AdvanceExpedienteMixin, TestCase):
    def test_advance_is_money_invoiced_but_not_goods(self):
        self.make_advance(Decimal("300.00"), "30")
        summary = self.summary()

        # Money figures count it...
        self.assertEqual(summary["total_invoiced"], Decimal("363.00"))
        self.assertEqual(summary["total_invoiced_subtotal_net"], Decimal("300.00"))
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("700.00"))
        # ...the goods figures do not.
        self.assertEqual(summary["total_invoiced_subtotal"], Decimal("0.00"))
        self.assertEqual(summary["remaining_subtotal"], Decimal("1000.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("1000.00"))
        self.assertTrue(summary["can_invoice"])

    def test_full_pending_amount_after_an_advance_bills_full_lines(self):
        self.make_advance(Decimal("300.00"), "30")
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            self.summary(), "percentage", Decimal("100"),
        )
        self.assertEqual(ratio, Decimal("1"))

    def test_advance_entry(self):
        invoice, line = self.make_advance(Decimal("300.00"), "30")
        summary = self.summary()

        self.assertEqual(summary["advances"], [{
            "id": invoice.pk,
            "invoice_number": invoice.display_number,
            "date_issued": date(2026, 9, 1),
            "status": SalesInvoiceStatus.FINAL,
            "percentage": Decimal("30.0000"),
            "base": Decimal("300.00"),
            "deducted": Decimal("0.00"),
            "pending": Decimal("300.00"),
            "paid": False,
            "deductible": True,
            "cancelling": False,
            "lines": [{
                "id": line.pk,
                "base": Decimal("300.00"),
                "deducted": Decimal("0.00"),
                "pending": Decimal("300.00"),
            }],
        }])
        self.assertEqual(summary["advances_open_net"], Decimal("300.00"))
        self.assertEqual(summary["advances_deductible_net"], Decimal("300.00"))

    def test_deduction_gives_the_goods_back(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods()
        self.deduct(rest, line, Decimal("300.00"))
        rest.refresh_from_db()
        self.assertEqual(rest.subtotal_amount.amount, Decimal("700.00"))
        self.assertEqual(rest.total_amount.amount, Decimal("847.00"))

        summary = self.summary()
        # 363.00 + 847.00: one invoice for the whole expediente.
        self.assertEqual(summary["total_invoiced"], Decimal("1210.00"))
        self.assertEqual(summary["remaining"], Decimal("0.00"))
        self.assertEqual(summary["total_invoiced_subtotal_net"], Decimal("1000.00"))
        self.assertEqual(summary["remaining_subtotal_net"], Decimal("0.00"))
        self.assertEqual(summary["total_invoiced_subtotal"], Decimal("1000.00"))
        self.assertEqual(summary["remaining_subtotal"], Decimal("0.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertFalse(summary["can_invoice"])
        # The rest is still a pro forma: its deduction already reserves the advance.
        self.assertEqual(summary["advances"][0]["deducted"], Decimal("300.00"))
        self.assertEqual(summary["advances"][0]["pending"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))

    def test_partial_deduction_leaves_the_rest_pending(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods(Decimal("0.5"))
        self.deduct(rest, line, Decimal("150.00"))

        summary = self.summary()
        self.assertEqual(summary["goods_remaining_net"], Decimal("500.00"))
        self.assertEqual(summary["advances"][0]["lines"][0]["pending"], Decimal("150.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("150.00"))


class CancelledInvoicesTests(AdvanceExpedienteMixin, TestCase):
    def test_cancelled_goods_invoice_bills_nothing(self):
        invoice = self.invoice_goods()
        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        summary = self.summary()
        self.assertEqual(summary["total_invoiced"], Decimal("0.00"))
        self.assertEqual(summary["total_invoiced_subtotal_net"], Decimal("0.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("1000.00"))
        self.assertTrue(summary["can_invoice"])
        self.assertEqual(summary["invoices"], [])

    def test_cancelled_bulk_invoice_bills_nothing(self):
        invoice = BulkProjectFileInvoiceService.create_bulk_invoice(
            [self.project_file], company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29),
        )
        self.assertFalse(self.summary()["can_invoice"])
        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        summary = self.summary()
        self.assertEqual(summary["total_invoiced_subtotal"], Decimal("0.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("1000.00"))
        self.assertTrue(summary["can_invoice"])

    def test_cancelled_rest_invoice_releases_the_advance(self):
        _, line = self.make_advance(Decimal("300.00"), "30")
        rest = self.invoice_goods()
        self.deduct(rest, line, Decimal("300.00"))
        self.set_status(rest, SalesInvoiceStatus.FINAL)
        self.set_status(rest, SalesInvoiceStatus.CANCELLED)

        summary = self.summary()
        self.assertEqual(summary["goods_remaining_net"], Decimal("1000.00"))
        self.assertEqual(summary["advances"][0]["deducted"], Decimal("0.00"))
        self.assertEqual(summary["advances_open_net"], Decimal("300.00"))

    def test_cancelled_advance_is_left_out(self):
        invoice, _ = self.make_advance(Decimal("300.00"), "30")
        self.set_status(invoice, SalesInvoiceStatus.CANCELLED)

        summary = self.summary()
        self.assertEqual(summary["advances"], [])
        self.assertEqual(summary["advances_open_net"], Decimal("0.00"))
        self.assertEqual(summary["total_invoiced"], Decimal("0.00"))


class AdvanceEligibilityTests(AdvanceExpedienteMixin, TestCase):
    def test_proforma_advance_is_open_but_not_deductible(self):
        self.make_advance(Decimal("300.00"), "30", status=SalesInvoiceStatus.PROFORMA)

        summary = self.summary()
        self.assertFalse(summary["advances"][0]["deductible"])
        self.assertEqual(summary["advances_open_net"], Decimal("300.00"))
        self.assertEqual(summary["advances_deductible_net"], Decimal("0.00"))

    def test_paid_flag(self):
        invoice, _ = self.make_advance(Decimal("300.00"), "30")
        self.assertFalse(self.summary()["advances"][0]["paid"])
        SalesInvoice.objects.filter(pk=invoice.pk).update(payment_status=InvoicePaymentStatus.PAID)
        self.assertTrue(self.summary()["advances"][0]["paid"])

    def test_cancellation_at_the_aeat_takes_the_advance_out_of_reach(self):
        invoice, _ = self.make_advance(Decimal("300.00"), "30")
        account = EInvoicingAccount.objects.create(company=self.company, name="Verifacti")
        EInvoicingSubmission.objects.create(
            account=account, sales_invoice=invoice,
            action=SubmissionAction.CREATE, status=SubmissionStatus.ACCEPTED,
        )
        # (status, deductible, cancelling: the AEAT has not answered yet)
        cases = [
            (SubmissionStatus.PENDING, False, True),
            (SubmissionStatus.SUBMITTED, False, True),
            (SubmissionStatus.ACCEPTED, False, False),
            (SubmissionStatus.REJECTED, True, False),
            (SubmissionStatus.ERROR, True, False),
        ]
        for status, deductible, cancelling in cases:
            with self.subTest(status=status):
                cancel = EInvoicingSubmission.objects.create(
                    account=account, sales_invoice=invoice,
                    action=SubmissionAction.CANCEL, status=status,
                )
                summary = self.summary()
                self.assertEqual(summary["advances"][0]["deductible"], deductible)
                self.assertEqual(summary["advances"][0]["cancelling"], cancelling)
                self.assertEqual(
                    summary["advances_deductible_net"],
                    Decimal("300.00") if deductible else Decimal("0.00"),
                )
                # Still open either way: it is not cancelled yet.
                self.assertEqual(summary["advances_open_net"], Decimal("300.00"))
                cancel.delete()


class GoodsByVatGroupTests(_GoldenExpedienteBase):
    """goods_remaining_net_by_group on the golden expediente: IVA 21 %,
    IVA 10 % and an untaxed material line, under a basket and a line-items
    financing discount."""

    def _groups(self):
        summary = ProjectFileInvoiceService.get_invoiceable_summary(
            self.project_file, with_groups=True,
        )
        groups = summary["goods_remaining_net_by_group"]
        self.assertEqual(sum(groups.values()), summary["goods_remaining_net"])
        return groups

    def test_only_on_request(self):
        self.assertNotIn("goods_remaining_net_by_group", self._summary())

    def test_whole_expediente(self):
        self.assertEqual(self._groups(), {
            frozenset({(self.iva, Decimal("21"))}): Decimal("1599.81"),
            frozenset({(self.iva, Decimal("10"))}): Decimal("344.43"),
            frozenset(): Decimal("81.06"),
        })

    def test_after_a_partial_invoice(self):
        # 40 %: IVA 21 % base 751.51, inventory items in full (IVA 10 %
        # 344.43), and 32.42 untaxed (1128.36 taxable − the two).
        self._create_invoice(self._items(Decimal("40")))
        self.assertEqual(self._groups(), {
            frozenset({(self.iva, Decimal("21"))}): Decimal("848.30"),
            frozenset({(self.iva, Decimal("10"))}): Decimal("0.00"),
            frozenset(): Decimal("48.64"),
        })

    def test_after_the_full_invoice(self):
        self._create_invoice(self._items(Decimal("100")))
        self.assertEqual(set(self._groups().values()), {Decimal("0.00")})


class GoodsByVatGroupWithAdvancesTests(AdvanceExpedienteMixin, TestCase):
    def test_advances_and_deductions_are_not_goods(self):
        self.add_config(self.quote, Decimal("500.00"), percentage=Decimal("10"))
        iva_21 = frozenset({(self.iva, Decimal("21"))})
        iva_10 = frozenset({(self.iva, Decimal("10"))})
        _, line = self.make_advance(Decimal("300.00"), "20")

        groups = self.summary(with_groups=True)["goods_remaining_net_by_group"]
        self.assertEqual(groups, {iva_21: Decimal("1000.00"), iva_10: Decimal("500.00")})

        rest = self.invoice_goods()
        self.deduct(rest, line, Decimal("300.00"))
        groups = self.summary(with_groups=True)["goods_remaining_net_by_group"]
        self.assertEqual(groups, {iva_21: Decimal("0.00"), iva_10: Decimal("0.00")})


class NetOverInvoicingCheckTests(AdvanceExpedienteMixin, TestCase):
    def test_goods_are_invoiced_in_full_after_a_full_advance(self):
        self.make_advance(Decimal("1000.00"), "100")
        invoice = self.invoice_goods()
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("1000.00"))

    def test_a_cancelled_invoice_does_not_block_its_replacement(self):
        self.set_status(self.invoice_goods(), SalesInvoiceStatus.CANCELLED)
        replacement = self.invoice_goods()
        self.assertEqual(replacement.subtotal_amount.amount, Decimal("1000.00"))

    def test_goods_beyond_the_base_are_refused(self):
        self.invoice_goods()
        with self.assertRaises(ValidationError):
            self.invoice_goods()
        self.assertEqual(SalesInvoice.objects.filter(project_file=self.project_file).count(), 1)

    def test_taxes_do_not_count(self):
        # A legacy invoice whose total added a 15 % IRPF instead of
        # subtracting it (500 + 105 + 75) does not block the rest: only
        # taxable bases count.
        first = self.invoice_goods(Decimal("0.5"))
        SalesInvoice.objects.filter(pk=first.pk).update(total_amount=Decimal("680.00"))

        second = self.invoice_goods(Decimal("0.5"))
        self.assertEqual(second.subtotal_amount.amount, Decimal("500.00"))
        summary = self.summary()
        self.assertEqual(summary["remaining"], Decimal("-75.00"))
        self.assertEqual(summary["goods_remaining_net"], Decimal("0.00"))
        self.assertFalse(summary["can_invoice"])

    def test_tolerance_is_a_thousandth_of_the_base(self):
        summary = self.summary()
        AdvanceDeductionService.check(summary, Decimal("1001.00"))
        with self.assertRaises(ValidationError):
            AdvanceDeductionService.check(summary, Decimal("1001.01"))
