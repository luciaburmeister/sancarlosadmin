from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from djmoney.money import Money

from banking.models import BankTransaction, BankTransactionCategory, ReconciliationStatus
from banking.services.transaction_matching.matcher import TransactionMatcher
from company.models import BankAccount, Company
from invoices.models import Invoice
from materials.models import Provider
from payments.models import Payment, PaymentAllocation, PaymentDirection


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
        self.category = BankTransactionCategory.objects.create(
            company=self.company, name="Purchases",
        )
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
            category=self.category,
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


class MatcherCandidateQueryCountTests(TestCase):
    """
    Regression test for the Sentry N+1 in run_matcher_for_transaction: the
    matcher must read each candidate's remaining amount from the queryset
    annotation, not run one SUM(amount_applied) per invoice.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Test Co")
        self.account = BankAccount.objects.create(
            company=self.company, bank_name="Test Bank",
            iban="ES0000000000000000000001",
        )
        self.provider = Provider.objects.create(company=self.company, name="ACME")

    def _make_invoice(self, number, total):
        return Invoice.objects.create(
            company=self.company,
            provider=self.provider,
            invoice_number=number,
            date_issued=date(2026, 1, 1),
            total_amount=Money(total, "EUR"),
            file=SimpleUploadedFile("invoice.pdf", b"dummy", content_type="application/pdf"),
            payment_status="unpaid",
        )

    def _count_matcher_queries(self, amount):
        txn = BankTransaction.objects.create(
            company=self.company, bank_account=self.account,
            external_id=f"q-{BankTransaction.objects.count()}",
            transaction_date=date(2026, 1, 5), amount=Money(amount, "EUR"),
            currency="EUR", raw_data={}, reconciliation_status=ReconciliationStatus.UNRECONCILED,
        )
        with CaptureQueriesContext(connection) as ctx:
            recon = TransactionMatcher(txn).run()
        return len(ctx.captured_queries), recon

    def test_query_count_does_not_grow_with_candidates(self):
        self._make_invoice("A-1", "100.00")
        baseline, _ = self._count_matcher_queries("-999.00")

        for i in range(2, 8):
            self._make_invoice(f"A-{i}", "100.00")
        with_more, _ = self._count_matcher_queries("-999.00")

        self.assertEqual(with_more, baseline)

    def test_annotated_remaining_accounts_for_partial_payments(self):
        invoice = self._make_invoice("P-1", "500.00")
        payment = Payment.objects.create(
            company=self.company, payment_date=date(2026, 1, 2),
            amount=Decimal("200.00"), amount_currency="EUR",
            direction=PaymentDirection.OUTBOUND, bank_account=self.account,
        )
        PaymentAllocation.objects.create(
            payment=payment,
            content_type=ContentType.objects.get_for_model(Invoice),
            object_id=invoice.pk,
            amount_applied=Decimal("200.00"),
        )

        _, recon = self._count_matcher_queries("-300.00")

        self.assertEqual(
            TransactionMatcher.get_selected_allocations(recon.suggested_allocations),
            [{"content_type": "invoice", "object_id": invoice.pk}],
        )
