"""Main orchestrator for bank transaction matching and reconciliation."""
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.utils import timezone
from djmoney.money import Money

from banking.models import (
    BankReconciliation,
    BankTransaction,
    ReconciliationMethod,
    ReconciliationStatus,
)
from banking.services.transaction_matching.amount_matching import match_amount
from banking.services.transaction_matching.counterparty import find_counterparty
from banking.services.transaction_matching.types import MatchResult
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentCreatedBy,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)


class TransactionMatcher:
    """
    Orchestrates matching a BankTransaction to unpaid documents.

    Usage:
        matcher = TransactionMatcher(bank_transaction)
        reconciliation = matcher.run()
    """

    def __init__(self, bank_transaction: BankTransaction):
        self.transaction = bank_transaction
        self.direction = self._get_direction()
        self.counterparty = None
        self.result: MatchResult | None = None

    def run(self) -> BankReconciliation:
        """
        Run the matching pipeline and create a BankReconciliation suggestion.
        Never creates a Payment — that only happens on human confirmation.
        """
        self.counterparty = find_counterparty(self.transaction, self.direction)
        candidates = self._get_candidate_documents()
        self.result = match_amount(self.transaction, candidates)

        suggested_allocations = [
            {
                "content_type": ContentType.objects.get_for_model(doc).model,
                "object_id": doc.pk,
                "match_confidence": self.result.confidence,
            }
            for doc in self.result.documents
        ]

        reconciliation = BankReconciliation.objects.create(
            bank_transaction=self.transaction,
            payment=None,
            is_confirmed=False,
            method=ReconciliationMethod.AUTO,
            suggested_allocations=suggested_allocations,
            unmatched_amount=Money(
                self.result.unmatched_amount,
                self.transaction.amount_currency,
            ),
        )

        return reconciliation

    @staticmethod
    @db_transaction.atomic
    def confirm(
        reconciliation: BankReconciliation,
        confirmed_allocations: list,
        user,
    ) -> BankReconciliation:
        """
        Confirm (or modify) a matcher suggestion and create the Payment.

        confirmed_allocations is a list of dicts:
            [{"content_type": "invoice", "object_id": 5}, ...]

        Steps:
        1. Determine if the human changed the suggestion (AUTO vs MANUAL)
        2. Create the Payment
        3. Create PaymentAllocations
        4. Link the Payment to the BankReconciliation
        5. Update statuses
        """
        txn = reconciliation.bank_transaction

        # Guard: don't re-create if already confirmed
        if reconciliation.is_confirmed and reconciliation.payment_id:
            return reconciliation

        # Step 1 — Did the human change the suggestion?
        method = TransactionMatcher._determine_method(
            reconciliation.suggested_allocations, confirmed_allocations,
        )

        # Step 2 — Get or create the Payment (idempotent)
        direction = (
            PaymentDirection.INBOUND
            if txn.amount.amount > 0
            else PaymentDirection.OUTBOUND
        )

        payment, _created = Payment.objects.get_or_create(
            company=txn.company,
            payment_date=txn.transaction_date,
            amount=abs(txn.amount.amount),
            amount_currency=txn.amount_currency,
            direction=direction,
            bank_account=txn.bank_account,
            created_by=PaymentCreatedBy.AUTO,
            defaults={
                "method": PaymentMethod.TRANSFER,
                "status": PaymentStatus.DRAFT,
            },
        )

        # Step 3 — Create PaymentAllocations
        for allocation in confirmed_allocations:
            ct = ContentType.objects.get(model=allocation["content_type"])
            doc = ct.get_object_for_this_type(pk=allocation["object_id"])
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ct,
                object_id=allocation["object_id"],
                amount_applied=doc.amount_remaining_decimal,
            )

        # Step 4 — Update the BankReconciliation
        reconciliation.payment = payment
        reconciliation.is_confirmed = True
        reconciliation.method = method
        reconciliation.reconciled_by = user
        reconciliation.confirmed_at = timezone.now()
        reconciliation.save(update_fields=[
            "payment",
            "is_confirmed",
            "method",
            "reconciled_by",
            "confirmed_at",
        ])

        # Step 5 — Update BankTransaction reconciliation status
        txn.reconciliation_status = ReconciliationStatus.RECONCILED
        txn.save(update_fields=["reconciliation_status"])

        return reconciliation

    # ------------------------------------------------------------------
    # Private methods
    # ------------------------------------------------------------------

    def _get_direction(self) -> str:
        """Infer payment direction from the transaction amount sign."""
        if self.transaction.amount.amount > 0:
            return PaymentDirection.INBOUND
        return PaymentDirection.OUTBOUND

    def _get_candidate_documents(self) -> list:
        """
        Fetch unpaid / partially paid documents that could match.
        Scoped to the identified counterparty when available.
        """
        Invoice = apps.get_model("invoices", "Invoice")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")

        if self.direction == PaymentDirection.OUTBOUND:
            qs = Invoice.objects.filter(
                company=self.transaction.company,
                payment_status__in=["unpaid", "partial"],
            )
            if self.counterparty:
                qs = qs.filter(provider=self.counterparty)
            return list(qs)

        qs = SalesInvoice.objects.filter(
            company=self.transaction.company,
            payment_status__in=["unpaid", "partial"],
        )
        if self.counterparty:
            qs = qs.filter(client=self.counterparty)
        return list(qs)

    @staticmethod
    def _determine_method(
        suggested_allocations: list,
        confirmed_allocations: list,
    ) -> str:
        """
        Compare what the matcher suggested vs what the human confirmed.
        Same → AUTO; different → MANUAL.
        """
        if not suggested_allocations:
            return ReconciliationMethod.MANUAL

        def as_set(allocs):
            return frozenset(
                (a["content_type"], int(a["object_id"]))
                for a in allocs
            )

        if as_set(suggested_allocations) == as_set(confirmed_allocations):
            return ReconciliationMethod.AUTO
        return ReconciliationMethod.MANUAL
