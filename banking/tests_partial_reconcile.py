from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationStatus
from banking.services.transaction_matching.matcher import TransactionMatcher
from company.models import BankAccount, Company
from invoices.models import Invoice
from materials.models import Provider
from payments.models import PaymentAllocation


class PartialManualReconciliationTests(TestCase):
    """
    Regression test for the SAN CARLOS case: a single invoice paid via
    several smaller bank transactions. Confirming an allocation for an
    invoice whose remaining balance exceeds the transaction amount must
    cap the applied amount instead of over-allocating.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test Bank",
            iban="ES0000000000000000000001",
        )
        self.provider = Provider.objects.create(company=self.company, name="SAN CARLOS")
        self.invoice = Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number="SC-001",
            date_issued=date(2026, 1, 1),
            total_amount=Money("3000.00", "EUR"),
            file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
            payment_status="unpaid",
        )

    def _make_txn(self, ext_id, amount):
        return BankTransaction.objects.create(
            company=self.company, bank_account=self.account, external_id=ext_id,
            transaction_date=date(2026, 1, 5), amount=Money(amount, "EUR"),
            currency="EUR", raw_data={}, reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )

    def _confirm(self, txn):
        recon = TransactionMatcher(txn).run()
        return TransactionMatcher.confirm(
            reconciliation=recon,
            confirmed_allocations=[{"content_type": "invoice", "object_id": self.invoice.pk}],
            user=None,
        )

    def test_partial_charge_is_capped_not_overallocated(self):
        txn1 = self._make_txn("sc-1", "-1000.00")
        self._confirm(txn1)

        self.invoice.refresh_from_db()
        txn1.refresh_from_db()

        allocated = PaymentAllocation.objects.filter(
            content_type__model="invoice", object_id=self.invoice.pk,
        ).count()

        self.assertEqual(allocated, 1)
        alloc = PaymentAllocation.objects.get(
            content_type__model="invoice", object_id=self.invoice.pk,
        )
        self.assertEqual(alloc.amount_applied.amount, Decimal("1000.00"))
        self.assertEqual(self.invoice.payment_status, "partial")
        self.assertEqual(self.invoice.amount_remaining_decimal, Decimal("2000.00"))
        self.assertEqual(txn1.reconciliation_status, ReconciliationStatus.RECONCILED)

    def test_three_installments_fully_settle_invoice(self):
        for i in range(3):
            txn = self._make_txn(f"sc-{i}", "-1000.00")
            self._confirm(txn)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.payment_status, "paid")
        self.assertEqual(self.invoice.amount_remaining_decimal, Decimal("0.00"))
        self.assertEqual(
            PaymentAllocation.objects.filter(
                content_type__model="invoice", object_id=self.invoice.pk,
            ).count(),
            3,
        )
