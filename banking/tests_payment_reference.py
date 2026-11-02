from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationMethod, ReconciliationStatus
from banking.services.transaction_matching.matcher import TransactionMatcher
from banking.tasks import run_matcher_for_transaction
from company.models import BankAccount
from invoices import payment_reference as payment_references
from invoices.models import Invoice, InvoiceDocumentType, SalesInvoice, SalesInvoiceStatus
from invoices.tests.factories import make_client, make_company
from materials.models import Provider
from payments.mixins import annotate_credit_balance
from payments.models import Payment, PaymentAllocation, PaymentDirection


class PaymentReferenceMatchingTests(TestCase):
    """A payment reference in the concept picks the invoice, whatever the IBAN."""

    def setUp(self):
        self.company = make_company()
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test Bank", iban="ES0000000000000000000001",
        )
        self.client_obj = make_client(company=self.company, name="Unrelated Name SL")
        self.provider = Provider.objects.create(company=self.company, name="ACME")

    def _sales_invoice(self, total="121.00", company=None, status=SalesInvoiceStatus.FINAL):
        company = company or self.company
        client = self.client_obj if company == self.company else make_client(company=company)
        return SalesInvoice.objects.create(
            company=company, client=client, status=status, date_issued=date(2026, 1, 1),
            total_amount=Money(total, "EUR"),
        )

    def _purchase_invoice(self, total="121.00", **fields):
        return Invoice.objects.create(
            company=self.company, provider=self.provider, date_issued=date(2026, 1, 1),
            total_amount=Money(total, "EUR"), payment_status="unpaid",
            file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
            **fields,
        )

    def _txn(self, amount, description, ext_id="t1"):
        return BankTransaction.objects.create(
            company=self.company, bank_account=self.account, external_id=ext_id,
            transaction_date=date(2026, 1, 10), amount=Money(amount, "EUR"), currency="EUR",
            description=description, counterparty_name="SOMEONE ELSE",
            counterparty_iban="ES9999999999999999999999", raw_data={},
            reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

    def _concept(self, invoice):
        return f"PAGO FRA {payment_references.display(invoice.payment_reference)}"

    def _selected(self, recon):
        return next(c for c in recon.suggested_allocations if c["is_selected"])

    def test_inbound_exact_amount_is_auto_confirmed(self):
        invoice = self._sales_invoice()
        # A same-amount invoice the amount matcher could have picked instead
        self._sales_invoice()
        txn = self._txn("121.00", self._concept(invoice))

        result = run_matcher_for_transaction(txn.pk)

        self.assertTrue(result["auto_confirmed"])
        invoice.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        allocation = PaymentAllocation.objects.get(content_type__model="salesinvoice")
        self.assertEqual(allocation.object_id, invoice.pk)
        self.assertEqual(allocation.payment.reconciliation.method, ReconciliationMethod.AUTO)

    def test_outbound_exact_amount_is_auto_confirmed(self):
        invoice = self._purchase_invoice()
        txn = self._txn("-121.00", f"Transferencia {invoice.payment_reference}")

        self.assertTrue(run_matcher_for_transaction(txn.pk)["auto_confirmed"])

        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")

    def _confirm(self, txn):
        recon = TransactionMatcher(txn).run()
        self.assertEqual(self._selected(recon)["confidence"], "reference")
        self.assertTrue(TransactionMatcher.auto_confirm(recon))
        recon.refresh_from_db()
        txn.refresh_from_db()
        return recon

    def test_outbound_partial_amount_is_applied_to_the_invoice(self):
        invoice = self._purchase_invoice()
        txn = self._txn("-100.00", self._concept(invoice))

        recon = self._confirm(txn)

        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "partial")
        self.assertEqual(invoice.amount_remaining_decimal, Decimal("21.00"))
        self.assertEqual(recon.unmatched_amount.amount, Decimal("0"))
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)

    def test_outbound_overpayment_leaves_credit_with_the_provider(self):
        invoice = self._purchase_invoice()
        txn = self._txn("-150.00", self._concept(invoice))

        recon = self._confirm(txn)

        invoice.refresh_from_db()
        payment = recon.payment
        payment.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        self.assertEqual(payment.provider, self.provider)
        self.assertEqual(payment.amount_pending_allocation.amount, Decimal("29.00"))
        self.assertEqual(recon.unmatched_amount.amount, Decimal("0"))
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        provider = annotate_credit_balance(
            Provider.objects.filter(pk=self.provider.pk), "provider", PaymentDirection.OUTBOUND,
        ).get()
        self.assertEqual(provider.credit_balance, Decimal("29.00"))

    def test_inbound_partial_and_overpayment_work_the_same(self):
        invoice = self._sales_invoice()
        self._confirm(self._txn("100.00", self._concept(invoice), ext_id="t1"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "partial")

        recon = self._confirm(self._txn("50.00", self._concept(invoice), ext_id="t2"))
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        recon.payment.refresh_from_db()
        self.assertEqual(recon.payment.client, self.client_obj)
        self.assertEqual(recon.payment.amount_pending_allocation.amount, Decimal("29.00"))

    def test_several_references_are_paid_in_the_order_written(self):
        first = self._purchase_invoice()
        second = self._purchase_invoice(total="100.00")
        txn = self._txn("-150.00", f"{first.payment_reference} {second.payment_reference}")

        self._confirm(txn)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.payment_status, "paid")
        self.assertEqual(second.amount_remaining_decimal, Decimal("71.00"))

    def test_counterparty_comes_from_the_referenced_invoice(self):
        invoice = self._sales_invoice()
        matcher = TransactionMatcher(self._txn("121.00", self._concept(invoice)))
        matcher.run()
        self.assertEqual(matcher.counterparty, self.client_obj)

    def _assert_reference_ignored(self, txn):
        recon = TransactionMatcher(txn).run()
        self.assertFalse(
            any(c["confidence"] == "reference" for c in recon.suggested_allocations)
        )
        self.assertFalse(TransactionMatcher.auto_confirm(recon))

    def test_paid_invoice_is_ignored(self):
        invoice = self._purchase_invoice()
        self._confirm(self._txn("-121.00", self._concept(invoice), ext_id="t1"))

        second = self._txn("-121.00", self._concept(invoice), ext_id="t2")
        self._assert_reference_ignored(second)
        self.assertEqual(Payment.objects.count(), 1)

    def test_invoice_paid_after_the_suggestion_is_not_confirmed(self):
        invoice = self._purchase_invoice()
        recon = TransactionMatcher(self._txn("-121.00", self._concept(invoice), ext_id="t1")).run()
        self._confirm(self._txn("-121.00", self._concept(invoice), ext_id="t2"))

        self.assertFalse(TransactionMatcher.auto_confirm(recon))
        self.assertEqual(Payment.objects.count(), 1)

    def test_cancelled_invoice_is_ignored(self):
        invoice = self._sales_invoice(status=SalesInvoiceStatus.CANCELLED)
        self._assert_reference_ignored(self._txn("121.00", self._concept(invoice)))

    def test_other_companys_invoice_is_ignored(self):
        invoice = self._sales_invoice(company=make_company(name="Other Co"))
        self._assert_reference_ignored(self._txn("121.00", self._concept(invoice)))

    def test_sales_reference_on_outbound_movement_is_ignored(self):
        invoice = self._sales_invoice()
        self._assert_reference_ignored(self._txn("-121.00", self._concept(invoice)))

    def test_purchase_credit_note_is_ignored(self):
        credit_note = self._purchase_invoice(document_type=InvoiceDocumentType.CREDIT_NOTE)
        self._assert_reference_ignored(self._txn("-121.00", self._concept(credit_note)))

    def test_no_reference_keeps_amount_matching(self):
        self._sales_invoice()
        recon = TransactionMatcher(self._txn("121.00", "TRANSFERENCIA")).run()
        self.assertTrue(recon.suggested_allocations)
        self.assertNotEqual(self._selected(recon)["confidence"], "reference")
        self.assertFalse(TransactionMatcher.auto_confirm(recon))
