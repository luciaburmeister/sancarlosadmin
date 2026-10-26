from datetime import date

from django.test import TestCase
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationStatus
from banking.services.transaction_matching.matcher import TransactionMatcher
from company.models import BankAccount
from finance.models_planned_tax_payment import PlannedTaxPayment, PlannedTaxPaymentDirection, TaxInstallment
from finance.services.payment_calendar import CalendarDirection, _tax_installment_entries
from invoices.models import TaxType
from invoices.tests.factories import make_company


class TaxRefundMatchingTests(TestCase):
    """A refund plan is matched on money coming in; a payment plan on money going out."""

    def setUp(self):
        self.company = make_company()
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test Bank", iban="ES0000000000000000000001",
        )
        self.vat = TaxType.objects.create(tax_name="IVA", country="ES", percentage_options=[21])

    def _installment(self, direction, amount="500.00"):
        plan = PlannedTaxPayment.objects.create(
            company=self.company, tax_type=self.vat, description="303 4T", direction=direction,
            drag_forward_if_unpaid=False,
        )
        return TaxInstallment.objects.create(
            planned_tax_payment=plan, due_date=date(2026, 1, 5), amount=Money(amount, "EUR"),
        )

    def _txn(self, amount):
        return BankTransaction.objects.create(
            company=self.company, bank_account=self.account, external_id=f"t{amount}",
            transaction_date=date(2026, 1, 10), amount=Money(amount, "EUR"), currency="EUR",
            description="AEAT", counterparty_name="AGENCIA TRIBUTARIA",
            counterparty_iban="ES9999999999999999999999", raw_data={},
            reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

    def _suggested_ids(self, txn):
        recon = TransactionMatcher(txn).run()
        return {
            alloc["object_id"]
            for combo in recon.suggested_allocations
            for alloc in combo["allocations"]
            if alloc["content_type"] == "taxinstallment"
        }

    def test_refund_is_suggested_for_inbound_movement(self):
        refund = self._installment(PlannedTaxPaymentDirection.REFUND)
        self._installment(PlannedTaxPaymentDirection.PAYMENT)

        self.assertEqual(self._suggested_ids(self._txn("500.00")), {refund.pk})

    def test_payment_is_suggested_for_outbound_movement(self):
        self._installment(PlannedTaxPaymentDirection.REFUND)
        payment = self._installment(PlannedTaxPaymentDirection.PAYMENT)

        self.assertEqual(self._suggested_ids(self._txn("-500.00")), {payment.pk})

    def test_refund_shows_as_income_in_calendar(self):
        self._installment(PlannedTaxPaymentDirection.REFUND)

        [entry] = _tax_installment_entries(self.company, date(2026, 1, 1), date(2026, 1, 31))

        self.assertEqual(entry.direction, CalendarDirection.INCOME)
        self.assertEqual(entry.category, "Tax refunds")
