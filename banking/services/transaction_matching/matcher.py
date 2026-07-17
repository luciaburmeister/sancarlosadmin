"""Main orchestrator for bank transaction matching and reconciliation."""
from decimal import Decimal

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
        self.result = match_amount(
            self.transaction,
            candidates,
            has_counterparty=self.counterparty is not None,
        )

        # Build the combined suggested_allocations structure:
        # [{"allocations": [...], "confidence": "exact", "is_selected": bool}, ...]
        all_combos = [self.result.documents] + self.result.alternatives
        suggested_allocations = [
            {
                "allocations": self._serialize_docs(docs),
                "confidence": self.result.confidence,
                "is_selected": idx == 0 and self.result.has_match,
            }
            for idx, docs in enumerate(all_combos)
            if docs  # skip empty
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
    def _serialize_docs(docs: list) -> list:
        """Convert a list of model instances to the allocation JSON format."""
        return [
            {
                "content_type": ContentType.objects.get_for_model(doc).model,
                "object_id": doc.pk,
            }
            for doc in docs
        ]

    @staticmethod
    def get_selected_allocations(suggested_allocations: list) -> list:
        """Extract the allocations list from the selected combination."""
        for combo in suggested_allocations:
            if combo.get("is_selected"):
                return combo.get("allocations", [])
        return []

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

        Idempotent — if the reconciliation is already confirmed, returns it.
        """
        if reconciliation.is_confirmed:
            return reconciliation

        txn = reconciliation.bank_transaction

        # Step 1 — Did the human change the suggestion?
        method = TransactionMatcher._determine_method(
            reconciliation.suggested_allocations, confirmed_allocations,
        )

        # Step 1 — Determine payment direction
        direction = (
            PaymentDirection.INBOUND
            if txn.amount.amount > 0
            else PaymentDirection.OUTBOUND
        )

        payment = Payment.objects.create(
            company=txn.company,
            payment_date=txn.transaction_date,
            amount=abs(txn.amount.amount),
            amount_currency=txn.amount_currency,
            direction=direction,
            bank_account=txn.bank_account,
            created_by=PaymentCreatedBy.AUTO,
            method=PaymentMethod.TRANSFER,
            status=PaymentStatus.CONFIRMED,
        )

        # Step 3 — Create PaymentAllocations
        #   Check the real-time remaining amount at confirmation time.
        #   Another reconciliation may have been confirmed in between,
        #   reducing or zeroing the remaining balance.
        total_allocated = Decimal("0")
        for allocation in confirmed_allocations:
            ct = ContentType.objects.get(model=allocation["content_type"])
            doc = ct.get_object_for_this_type(pk=allocation["object_id"])
            remaining = doc.amount_remaining_decimal
            if remaining <= 0:
                continue  # Already fully paid by another reconciliation
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ct,
                object_id=allocation["object_id"],
                amount_applied=remaining,
            )
            total_allocated += remaining

        # Step 4 — Update the BankReconciliation
        txn_amount = abs(txn.amount.amount)
        actual_unmatched = max(Decimal("0"), txn_amount - total_allocated)
        reconciliation.payment = payment
        reconciliation.is_confirmed = True
        reconciliation.method = method
        reconciliation.reconciled_by = user
        reconciliation.confirmed_at = timezone.now()
        reconciliation.unmatched_amount = Money(actual_unmatched, txn.amount_currency)
        reconciliation.save()

        # Step 5 — Update BankTransaction reconciliation status
        is_partial = confirmed_allocations and total_allocated < txn_amount
        txn.reconciliation_status = (
            ReconciliationStatus.PARTIAL if is_partial else ReconciliationStatus.RECONCILED
        )
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
        Only considers invoices issued on or before the transaction date.
        """
        Invoice = apps.get_model("invoices", "Invoice")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        LoanInstallment = apps.get_model("loans", "LoanInstallment")
        LoanEvent = apps.get_model("loans", "LoanEvent")
        txn_date = self.transaction.transaction_date

        if self.direction == PaymentDirection.OUTBOUND:
            qs = Invoice.objects.filter(
                company=self.transaction.company,
                payment_status__in=["unpaid", "partial"],
                date_issued__lte=txn_date,
            )
            if self.counterparty:
                qs = qs.filter(provider=self.counterparty)

            installment_qs = LoanInstallment.objects.filter(
                loan__company=self.transaction.company,
                loan__bank_account=self.transaction.bank_account,
                payment_status__in=["unpaid", "partial"],
                due_date__lte=txn_date,
            )

            # LoanEvents (early repayments) that haven't been reconciled yet
            from django.contrib.contenttypes.models import ContentType
            from payments.models import PaymentAllocation
            ct = ContentType.objects.get_for_model(LoanEvent)
            reconciled_event_ids = PaymentAllocation.objects.filter(
                content_type=ct
            ).values_list("object_id", flat=True)
            event_qs = LoanEvent.objects.filter(
                parent_loan__bank_account=self.transaction.bank_account,
                parent_loan__company=self.transaction.company,
                event_type="early_repayment",
                date__lte=txn_date,
            ).exclude(pk__in=reconciled_event_ids)

            return list(qs) + list(installment_qs) + list(event_qs)

        qs = SalesInvoice.objects.filter(
            company=self.transaction.company,
            payment_status__in=["unpaid", "partial"],
            date_issued__lte=txn_date,
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
        selected = TransactionMatcher.get_selected_allocations(suggested_allocations)
        if not selected:
            return ReconciliationMethod.MANUAL

        def as_set(allocs):
            return frozenset(
                (a["content_type"], int(a["object_id"]))
                for a in allocs
            )

        if as_set(selected) == as_set(confirmed_allocations):
            return ReconciliationMethod.AUTO
        return ReconciliationMethod.MANUAL