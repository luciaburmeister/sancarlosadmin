from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationMethod, ReconciliationStatus
from banking.services.transaction_matching.direct_debit import due_dates_charged_on, is_bank_business_day
from banking.services.transaction_matching.matcher import TransactionMatcher
from banking.tasks import match_waiting_charge_task, run_matcher_for_transaction
from company.models import BankAccount
from invoices.models import Invoice, InvoiceDocumentType, PaymentMethod as InvoicePaymentMethod
from invoices.tests.factories import OCR_DEFAULTS, OcrTestMixin, make_company
from materials.models import Provider
from payments.models import Payment, PaymentAllocation, PaymentMethod


class BankBusinessDayTests(SimpleTestCase):
    def test_weekends_and_target_holidays_are_closed(self):
        self.assertTrue(is_bank_business_day(date(2026, 10, 30)))  # Friday
        self.assertFalse(is_bank_business_day(date(2026, 10, 31)))  # Saturday
        self.assertFalse(is_bank_business_day(date(2026, 4, 3)))  # Good Friday
        self.assertFalse(is_bank_business_day(date(2026, 4, 6)))  # Easter Monday
        self.assertFalse(is_bank_business_day(date(2026, 12, 25)))
        # A Spanish holiday SEPA still settles on
        self.assertTrue(is_bank_business_day(date(2026, 12, 8)))

    def test_monday_collects_the_weekend(self):
        self.assertEqual(
            due_dates_charged_on(date(2026, 11, 2)),
            [date(2026, 11, 2), date(2026, 11, 1), date(2026, 10, 31)],
        )

    def test_monday_after_christmas_collects_the_holiday_too(self):
        self.assertEqual(
            due_dates_charged_on(date(2026, 12, 28)),
            [date(2026, 12, 28), date(2026, 12, 27), date(2026, 12, 26), date(2026, 12, 25)],
        )


class DirectDebitTestCase(TestCase):
    def setUp(self):
        self.company = make_company()
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test Bank", iban="ES0000000000000000000001",
        )
        self.provider = Provider.objects.create(company=self.company, name="Suministros Eléctricos")

    def _invoice(self, total="121.00", due_date=date(2026, 10, 30), company=None, provider=None, **fields):
        return Invoice.objects.create(
            company=company or self.company, provider=provider or self.provider, date_issued=date(2026, 10, 1),
            due_date=due_date, total_amount=Money(total, "EUR"), payment_status="unpaid",
            file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
            **fields,
        )

    def _txn(self, amount="-121.00", charged_on=date(2026, 10, 30), ext_id="t1"):
        # What the bank sends for a recibo: no IBAN, a payee name that is not the provider's
        return BankTransaction.objects.create(
            company=self.company, bank_account=self.account, external_id=ext_id,
            transaction_date=charged_on, amount=Money(amount, "EUR"), currency="EUR",
            description="ADEUDO A SU CARGO // PAGO DE ADEUDO DIRECTO SEPA // N 2026258000880361",
            counterparty_name="", counterparty_iban="", raw_data={},
            reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

    def _assert_reconciled(self, invoice, txn):
        result = run_matcher_for_transaction(txn.pk)

        self.assertTrue(result["auto_confirmed"])
        invoice.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        allocation = PaymentAllocation.objects.get()
        self.assertEqual(allocation.object_id, invoice.pk)
        self.assertEqual(allocation.payment.reconciliation.method, ReconciliationMethod.AUTO)

    def _assert_not_confirmed(self, txn):
        recon = TransactionMatcher(txn).run()
        self.assertFalse(any(c["confidence"] == "direct_debit" for c in recon.suggested_allocations))
        self.assertFalse(TransactionMatcher.auto_confirm(recon))
        self.assertFalse(Payment.objects.exists())


class DirectDebitMatchingTests(DirectDebitTestCase):
    """A charge on an invoice's due date, for exactly what it owes, reconciles itself."""

    def test_charge_on_the_due_date_is_reconciled(self):
        invoice = self._invoice()
        self._assert_reconciled(invoice, self._txn())

    def test_payment_method_of_the_invoice_does_not_matter(self):
        invoice = self._invoice(payment_method=InvoicePaymentMethod.TRANSFER)
        self._assert_reconciled(invoice, self._txn())

    def test_weekend_due_date_charged_on_monday_is_reconciled(self):
        invoice = self._invoice(due_date=date(2026, 10, 31))
        self._assert_reconciled(invoice, self._txn(charged_on=date(2026, 11, 2)))

    def test_holiday_due_date_charged_on_next_business_day_is_reconciled(self):
        invoice = self._invoice(due_date=date(2026, 12, 25))
        self._assert_reconciled(invoice, self._txn(charged_on=date(2026, 12, 28)))

    def test_partially_paid_invoice_charged_for_the_rest_is_reconciled(self):
        invoice = self._invoice()
        Payment.objects.create(
            company=self.company, payment_date=date(2026, 10, 15), amount=Money("21.00", "EUR"),
            direction="outbound", method=PaymentMethod.TRANSFER, status="confirmed",
        ).allocations.create(related_object=invoice, amount_applied=Decimal("21.00"))
        recon = TransactionMatcher(self._txn("-100.00")).run()

        self.assertTrue(TransactionMatcher.auto_confirm(recon))
        invoice.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")

    def test_charge_after_a_business_due_date_is_not_reconciled(self):
        self._invoice(due_date=date(2026, 10, 29))
        self._assert_not_confirmed(self._txn())

    def test_different_amount_is_not_reconciled(self):
        self._invoice()
        self._assert_not_confirmed(self._txn("-120.00"))

    def test_other_companys_invoice_is_not_reconciled(self):
        other = make_company(name="Other Co")
        self._invoice(company=other, provider=Provider.objects.create(company=other, name="Suministros Eléctricos"))
        self._assert_not_confirmed(self._txn())

    def test_same_charge_for_two_providers_is_left_for_a_human(self):
        self._invoice()
        self._invoice(provider=Provider.objects.create(company=self.company, name="Otro Proveedor"))
        self._assert_not_confirmed(self._txn())

    def test_credit_note_is_not_reconciled(self):
        self._invoice(document_type=InvoiceDocumentType.CREDIT_NOTE)
        self._assert_not_confirmed(self._txn())

    def test_two_identical_invoices_are_left_for_a_human(self):
        self._invoice()
        self._invoice()
        self._assert_not_confirmed(self._txn())

    def test_invoice_paid_after_the_suggestion_is_not_confirmed(self):
        invoice = self._invoice()
        recon = TransactionMatcher(self._txn()).run()
        self.assertEqual(
            next(c for c in recon.suggested_allocations if c["is_selected"])["confidence"], "direct_debit",
        )
        Payment.objects.create(
            company=self.company, payment_date=date(2026, 10, 29), amount=Money("121.00", "EUR"),
            direction="outbound", method=PaymentMethod.TRANSFER, status="confirmed",
        ).allocations.create(related_object=invoice, amount_applied=Decimal("121.00"))

        self.assertFalse(TransactionMatcher.auto_confirm(recon))
        self.assertEqual(Payment.objects.count(), 1)


class WaitingChargeTests(OcrTestMixin, DirectDebitTestCase):
    """The charge reached the bank first: receiving its invoice reconciles it."""

    def _charge_before_invoice(self, **txn_fields):
        txn = self._txn(**txn_fields)
        # The matcher ran on import, with no invoice to match it to yet
        self.assertFalse(run_matcher_for_transaction(txn.pk)["auto_confirmed"])
        return txn

    def _assert_waiting_charge_reconciled(self, invoice, txn):
        result = match_waiting_charge_task(invoice.pk)

        self.assertTrue(result["auto_confirmed"])
        invoice.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(invoice.payment_status, "paid")
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        self.assertEqual(PaymentAllocation.objects.get().object_id, invoice.pk)

    def _assert_waiting_charge_left(self, invoice):
        self.assertEqual(match_waiting_charge_task(invoice.pk), {"status": "no_match"})
        self.assertFalse(Payment.objects.exists())

    def test_invoice_received_after_its_charge_reconciles_it(self):
        txn = self._charge_before_invoice()
        self._assert_waiting_charge_reconciled(self._invoice(), txn)

    def test_weekend_due_date_finds_the_monday_charge(self):
        txn = self._charge_before_invoice(charged_on=date(2026, 11, 2))
        self._assert_waiting_charge_reconciled(self._invoice(due_date=date(2026, 10, 31)), txn)

    def test_ocr_receiving_the_invoice_reconciles_its_charge(self):
        self.company.tax_identifier = OCR_DEFAULTS["company_tax_id"]
        self.company.save(update_fields=["tax_identifier"])
        txn = self._charge_before_invoice()

        with self.captureOnCommitCallbacks(execute=True):
            invoice = self._process_new(total_amount=Decimal("121.00"), due_date="2026-10-30")

        txn.refresh_from_db()
        self.assertEqual(txn.reconciliation_status, ReconciliationStatus.RECONCILED)
        self.assertEqual(PaymentAllocation.objects.get().object_id, invoice.pk)

    def test_two_identical_charges_are_left_for_a_human(self):
        self._charge_before_invoice(ext_id="t1")
        self._charge_before_invoice(ext_id="t2")
        self._assert_waiting_charge_left(self._invoice())

    def test_charge_another_invoice_also_fits_is_left_for_a_human(self):
        self._charge_before_invoice()
        invoice = self._invoice()
        self._invoice(provider=Provider.objects.create(company=self.company, name="Otro Proveedor"))
        self._assert_waiting_charge_left(invoice)

    def test_reconciled_charge_is_not_taken_again(self):
        first = self._invoice()
        self._assert_reconciled(first, self._txn())
        second = self._invoice(invoice_number="2")

        self.assertEqual(match_waiting_charge_task(second.pk), {"status": "no_match"})
        self.assertEqual(Payment.objects.count(), 1)

    def test_charge_on_another_day_is_not_taken(self):
        self._charge_before_invoice(charged_on=date(2026, 11, 2))
        self._assert_waiting_charge_left(self._invoice())
