"""
Bank movements of an intercompany payment, registered before they reach
the bank: the payer's movement is the payment already created (found by the
paid invoice's reference), and the reimbursement's movements find the debit
note and its mirror by the reference they share.
"""
from django.test import TestCase
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationMethod, ReconciliationStatus
from banking.services.transaction_matching.matcher import TransactionMatcher
from banking.tasks import run_matcher_for_transaction
from invoices import payment_reference as payment_references
from invoices.tests.factories import make_bank_account
from invoices.tests.intercompany_fixtures import PAID_ON, IntercompanyMixin
from payments.models import Payment, PaymentAllocation


class IntercompanyMatchingTests(IntercompanyMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.record = self.register()

    def txn(self, amount, description, company=None, bank_account=None, ext_id="t1"):
        return BankTransaction.objects.create(
            company=company or self.payer, bank_account=bank_account or self.payer_bank, external_id=ext_id,
            transaction_date=PAID_ON, amount=Money(amount, "EUR"), currency="EUR", description=description,
            counterparty_name="PERFILES SA", counterparty_iban="ES9999999999999999999999", raw_data={},
            reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

    def concept(self):
        return f"TRANSF PERFILES {payment_references.display(self.invoice.payment_reference)}"

    def selected(self, txn):
        txn.reconciliation.refresh_from_db()
        return next(c for c in txn.reconciliation.suggested_allocations if c["is_selected"])

    def test_the_payers_movement_is_the_payment_registered_for_it(self):
        payments = Payment.objects.count()
        txn = self.txn("-1210.00", self.concept())

        result = run_matcher_for_transaction(txn.pk)

        self.assertTrue(result["auto_confirmed"])
        txn.refresh_from_db()
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        self.assertEqual(txn.reconciliation.payment, self.record.payer_payment)
        self.assertEqual(txn.reconciliation.method, ReconciliationMethod.AUTO)
        self.assertEqual(Payment.objects.count(), payments)

    def test_another_amount_is_only_suggested(self):
        txn = self.txn("-1200.00", self.concept())

        result = run_matcher_for_transaction(txn.pk)

        self.assertFalse(result["auto_confirmed"])
        selected = self.selected(txn)
        self.assertEqual((selected["confidence"], selected["payment_id"]), ("payment", self.record.payer_payment_id))

    def test_confirming_the_suggestion_links_the_payment(self):
        txn = self.txn("-1200.00", self.concept())
        run_matcher_for_transaction(txn.pk)

        TransactionMatcher.confirm_existing_payment(txn, self.record.payer_payment, user=None)

        txn.refresh_from_db()
        self.assertEqual(txn.reconciliation.payment, self.record.payer_payment)
        self.assertTrue(txn.reconciliation.is_confirmed)

    def test_a_movement_of_another_bank_account_is_not_it(self):
        other = make_bank_account(company=self.payer, iban="ES0000000000000000009999")
        txn = self.txn("-1210.00", self.concept(), bank_account=other)

        run_matcher_for_transaction(txn.pk)

        txn.refresh_from_db()
        self.assertFalse(any(c.get("payment_id") for c in txn.reconciliation.suggested_allocations))

    def test_the_invoice_number_instead_of_the_reference_is_a_suggestion(self):
        self.record.payer_payment.reference = "FRA-2026-0077"
        self.record.payer_payment.save(update_fields=["reference"])
        txn = self.txn("-1210.00", "PAGO FRA 2026 0077 PERFILES")

        result = run_matcher_for_transaction(txn.pk)

        self.assertFalse(result["auto_confirmed"])
        selected = self.selected(txn)
        self.assertEqual(selected["confidence"], "payment_named")
        self.assertEqual(selected["payment_id"], self.record.payer_payment_id)

    def test_the_debtors_repayment_settles_the_debt_to_the_payer(self):
        debtor_bank = make_bank_account(company=self.debtor, iban="ES0000000000000000001111")
        reference = payment_references.display(self.record.mirror_invoice.payment_reference)
        txn = self.txn("-1210.00", f"DEVOLUCION {reference}", company=self.debtor, bank_account=debtor_bank)

        self.assertTrue(run_matcher_for_transaction(txn.pk)["auto_confirmed"])

        allocation = PaymentAllocation.objects.get(payment=txn.reconciliation.payment)
        self.assertEqual(allocation.related_object, self.record.mirror_invoice)

    def test_the_payers_receipt_settles_its_debit_note(self):
        reference = payment_references.display(self.record.debit_note.payment_reference)
        txn = self.txn("1210.00", f"DEVOLUCION {reference}", ext_id="t2")

        self.assertTrue(run_matcher_for_transaction(txn.pk)["auto_confirmed"])

        allocation = PaymentAllocation.objects.get(payment=txn.reconciliation.payment)
        self.assertEqual(allocation.related_object, self.record.debit_note)

    def test_the_settlement_moved_no_money_through_any_bank(self):
        debtor_bank = make_bank_account(company=self.debtor, iban="ES0000000000000000001111")
        txn = self.txn("-1210.00", "x", company=self.debtor, bank_account=debtor_bank)

        with self.assertRaises(ValueError):
            TransactionMatcher.confirm_existing_payment(txn, self.record.settlement_payment, user=None)
