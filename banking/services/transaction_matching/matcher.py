"""Main orchestrator for bank transaction matching and reconciliation."""
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from banking.models import (
    BankReconciliation,
    BankTransaction,
    BankTransactionCategory,
    ReconciliationMethod,
    ReconciliationStatus,
)
from banking.services.transaction_matching import candidates, reference
from banking.services.transaction_matching.amount_matching import _remaining, match_amount
from banking.services.transaction_matching.counterparty import (
    find_counterparty,
    find_employee_counterparty,
)
from banking.services.transaction_matching.types import (
    PAYMENT_DATE_TOLERANCE_DAYS,
    MatchResult,
)
from payments.mixins import annotate_amount_remaining
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
        self.employee_counterparty = None
        self.result: MatchResult | None = None

    def run(self) -> BankReconciliation:
        """
        Run the matching pipeline and create a BankReconciliation suggestion.
        Never creates a Payment — that only happens on confirmation (human,
        or auto_confirm_if_referenced).

        Invoices whose payment reference is in the concept come first and
        selected; amount matching still runs and fills the alternatives.
        """
        referenced = reference.find_referenced_documents(self.transaction, self.direction)
        self.counterparty = (
            referenced and reference.counterparty_of(referenced)
        ) or find_counterparty(self.transaction, self.direction)
        candidates = self._get_candidate_documents()
        self.result = match_amount(
            self.transaction,
            candidates,
            has_counterparty=self.counterparty is not None or self.employee_counterparty is not None,
        )

        # Build the combined suggested_allocations structure:
        # [{"allocations": [...], "confidence": "exact", "is_selected": bool}, ...]
        all_combos = [self.result.documents] + self.result.alternatives
        suggested_allocations = [
            {
                "allocations": self._serialize_docs(docs),
                "confidence": self.result.confidence,
                "is_selected": idx == 0 and self.result.has_match and not referenced,
            }
            for idx, docs in enumerate(all_combos)
            if docs  # skip empty
        ]
        unmatched_amount = self.result.unmatched_amount

        if referenced:
            referenced_allocations = self._serialize_docs(referenced)
            suggested_allocations = [
                {
                    "allocations": referenced_allocations,
                    "confidence": "reference",
                    "is_selected": True,
                },
                *(combo for combo in suggested_allocations if combo["allocations"] != referenced_allocations),
            ]
            remaining = sum((_remaining(doc) for doc in referenced), Decimal("0"))
            unmatched_amount = max(Decimal("0"), abs(self.transaction.amount.amount) - remaining)

        reconciliation = BankReconciliation.objects.create(
            bank_transaction=self.transaction,
            payment=None,
            is_confirmed=False,
            method=ReconciliationMethod.AUTO,
            suggested_allocations=suggested_allocations,
            unmatched_amount=Money(
                unmatched_amount,
                self.transaction.amount_currency,
            ),
        )

        return reconciliation

    @staticmethod
    def auto_confirm_if_referenced(reconciliation: BankReconciliation) -> bool:
        """
        Confirm a suggestion with no human when the payer named the invoices
        by payment reference and the amount settles them exactly. Partial and
        over payments stay as suggestions. Returns whether it confirmed.
        """
        selected = next(
            (combo for combo in reconciliation.suggested_allocations if combo.get("is_selected")),
            None,
        )
        if not selected or selected.get("confidence") != "reference":
            return False

        remaining = Decimal("0")
        for allocation in selected["allocations"]:
            ct = ContentType.objects.get(model=allocation["content_type"])
            remaining += ct.get_object_for_this_type(pk=allocation["object_id"]).amount_remaining_decimal
        if remaining != abs(reconciliation.bank_transaction.amount.amount):
            return False

        TransactionMatcher.confirm(reconciliation, selected["allocations"], user=None)
        return True

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
        category: BankTransactionCategory | None = None,
    ) -> BankReconciliation:
        """
        Confirm (or modify) a matcher suggestion and create the Payment.

        confirmed_allocations is a list of dicts:
            [{"content_type": "invoice", "object_id": 5}, ...]

        If `category` is given, it's assigned to the bank transaction so
        confirming a reconciliation (manual or automatic) also categorizes
        it. Leaving it None keeps whatever category the transaction already
        had. It's only required with no documents: then the category's
        account is what the payment is booked to.

        Idempotent — if the reconciliation is already confirmed, returns it.
        """
        if reconciliation.is_confirmed:
            return reconciliation

        txn = reconciliation.bank_transaction

        if not confirmed_allocations and category is None and txn.category_id is None:
            raise ValueError(_("A movement with no document needs a category to be booked to."))

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
        #   Each allocation is capped at what's left of the transaction's
        #   own amount, so a document whose remaining balance is larger
        #   than the transaction (e.g. an invoice paid in installments)
        #   gets partially applied instead of over-allocated.
        txn_amount = abs(txn.amount.amount)
        total_allocated = Decimal("0")
        for allocation in confirmed_allocations:
            budget = txn_amount - total_allocated
            if budget <= 0:
                break
            ct = ContentType.objects.get(model=allocation["content_type"])
            doc = ct.get_object_for_this_type(pk=allocation["object_id"])
            remaining = doc.amount_remaining_decimal
            if remaining <= 0:
                continue  # Already fully paid by another reconciliation
            amount_to_apply = min(remaining, budget)
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ct,
                object_id=allocation["object_id"],
                amount_applied=amount_to_apply,
            )
            total_allocated += amount_to_apply

        # Step 4 — Update the BankReconciliation
        actual_unmatched = max(Decimal("0"), txn_amount - total_allocated)
        reconciliation.payment = payment
        reconciliation.is_confirmed = True
        reconciliation.method = method
        reconciliation.reconciled_by = user
        reconciliation.confirmed_at = timezone.now()
        reconciliation.unmatched_amount = Money(actual_unmatched, txn.amount_currency)
        reconciliation.save()

        # Step 5 — Update BankTransaction reconciliation status (+ category)
        is_partial = confirmed_allocations and total_allocated < txn_amount
        txn.reconciliation_status = (
            ReconciliationStatus.PARTIAL if is_partial else ReconciliationStatus.RECONCILED
        )
        update_fields = ["reconciliation_status"]
        if category is not None:
            txn.category = category
            update_fields.append("category")
        txn.save(update_fields=update_fields)

        return reconciliation

    @staticmethod
    @db_transaction.atomic
    def confirm_without_documents(
        txn: BankTransaction,
        category: BankTransactionCategory,
        user,
    ) -> BankReconciliation:
        """
        Reconcile a movement no document will ever back (bank fees, interest,
        transfers between own accounts): the whole amount becomes a payment
        with nothing allocated, which the ledger books to the category's
        account. A category without an account is refused, or the payment
        could never be journalized.
        """
        if not category.account_id:
            raise ValueError(_("“%(category)s” has no accounting account: set one on the category first.")
                             % {"category": category.name})
        if txn.reconciliation_status != ReconciliationStatus.UNRECONCILED:
            raise ValueError(_("Only unreconciled transactions can be booked to a category."))

        BankReconciliation.objects.filter(bank_transaction=txn, is_confirmed=False).delete()
        reconciliation = BankReconciliation.objects.create(
            bank_transaction=txn,
            is_confirmed=False,
            method=ReconciliationMethod.MANUAL,
            suggested_allocations=[],
            unmatched_amount=Money(0, txn.amount_currency),
        )
        TransactionMatcher.confirm(reconciliation, [], user, category=category)
        # Nothing is left to match: the category accounts for all of it
        reconciliation.unmatched_amount = Money(0, txn.amount_currency)
        reconciliation.save(update_fields=["unmatched_amount"])
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
        Only considers invoices issued on or before the transaction date,
        allowing for a payment to arrive up to PAYMENT_DATE_TOLERANCE_DAYS
        before the document's date (early payments).
        """
        Invoice = apps.get_model("invoices", "Invoice")
        SalesInvoice = apps.get_model("invoices", "SalesInvoice")
        LoanEvent = apps.get_model("loans", "LoanEvent")
        txn_date = self.transaction.transaction_date
        cutoff_date = txn_date + timedelta(days=PAYMENT_DATE_TOLERANCE_DAYS)

        # Querysets whose remaining amount is a SUM over PaymentAllocation
        # are annotated with db_amount_remaining (read by match_amount) so
        # the matcher doesn't run one aggregate per candidate.
        if self.direction == PaymentDirection.OUTBOUND:
            qs = annotate_amount_remaining(
                candidates.invoice_candidates(self.transaction.company, cutoff_date, self.counterparty),
                Invoice,
            )

            installment_qs = candidates.loan_installment_candidates(
                self.transaction.company, self.transaction.bank_account, cutoff_date
            ).select_related("loan")

            event_qs = candidates.loan_event_candidates(
                self.transaction.company, self.transaction.bank_account, cutoff_date
            ).select_related("parent_loan")
            event_qs = annotate_amount_remaining(event_qs, LoanEvent, amount_field="amount")

            # Approved expense claims go to an employee, not a provider, so
            # counterparty resolution is separate from self.counterparty
            # (which is Provider/Client-typed).
            employee = find_employee_counterparty(self.transaction, self.transaction.company)
            self.employee_counterparty = employee
            claim_qs = candidates.expense_claim_candidates(self.transaction.company, cutoff_date, employee)

            payroll_qs = candidates.employee_payroll_candidates(self.transaction.company, cutoff_date)

            tax_payment_qs = candidates.planned_tax_payment_candidates(self.transaction.company, cutoff_date)
            tax_payments = [p for p in tax_payment_qs if not p.is_refund]

            return (
                list(qs) + list(installment_qs) + list(event_qs)
                + list(claim_qs) + list(payroll_qs) + tax_payments
            )

        qs = candidates.sales_invoice_candidates(self.transaction.company, cutoff_date, self.counterparty)

        tax_payment_qs = candidates.planned_tax_payment_candidates(self.transaction.company, cutoff_date)
        tax_refunds = [p for p in tax_payment_qs if p.is_refund]

        return list(annotate_amount_remaining(qs, SalesInvoice)) + tax_refunds

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