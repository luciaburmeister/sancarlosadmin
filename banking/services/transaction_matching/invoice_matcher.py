"""Reverse matcher: starting from an Invoice/SalesInvoice/ExpenseClaim, find matching BankTransactions."""
import logging
from datetime import timedelta
from decimal import Decimal
from itertools import combinations as itertools_combinations

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.utils import timezone
from djmoney.money import Money

from banking.models import (
    BankReconciliation,
    BankTransaction,
    BankTransactionCategory,
    InvoiceReconciliationSuggestion,
    ReconciliationMethod,
    ReconciliationStatus,
)
from banking.services.transaction_matching.types import (
    AMOUNT_TOLERANCE_PERCENT,
    PAYMENT_DATE_TOLERANCE_DAYS,
)
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentCreatedBy,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)

logger = logging.getLogger(__name__)

MAX_COMBO_WITH_COUNTERPARTY = 10
MAX_COMBO_WITHOUT_COUNTERPARTY = 3
MAX_ALTERNATIVES = 20

# Expense claims have no counterparty IBAN to narrow candidates by, so the
# combination search would otherwise scan every unreconciled outbound
# transaction in the company (O(n³)). Bound the pool by time instead:
# reimbursements happen within weeks of approval, not months.
CLAIM_MATCH_WINDOW_DAYS = 90


class InvoiceMatcher:
    """
    Reverse of TransactionMatcher: given an Invoice, SalesInvoice, or
    ExpenseClaim, finds BankTransactions whose amounts sum to the
    document's remaining balance.

    Usage:
        matcher = InvoiceMatcher(invoice)
        suggestion = matcher.run()   # creates InvoiceReconciliationSuggestion
        InvoiceMatcher.confirm(suggestion, selected_combo, user, category=category)  # creates Payment etc.
    """

    def __init__(self, invoice):
        self.invoice = invoice
        self.invoice_type = invoice._meta.model_name  # "invoice" or "salesinvoice"
        self.direction = (
            PaymentDirection.INBOUND
            if self.invoice_type == "salesinvoice"
            else PaymentDirection.OUTBOUND
        )
        self.has_counterparty = False

    def run(self) -> InvoiceReconciliationSuggestion:
        """
        Find matching bank transactions and create an InvoiceReconciliationSuggestion.
        Never creates Payment records — that only happens on human confirmation.
        Works for both unpaid/partial invoices (will create a new Payment on confirm)
        and fully paid invoices (will link the existing Payment to the bank transaction).
        """
        candidates = self._get_candidate_transactions()
        combinations = self._find_matching_combos(candidates)

        ct = ContentType.objects.get_for_model(self.invoice)
        suggestion = InvoiceReconciliationSuggestion.objects.create(
            content_type=ct,
            object_id=self.invoice.pk,
            company=self.invoice.company,
            suggested_combinations=combinations,
        )
        return suggestion

    @staticmethod
    @db_transaction.atomic
    def confirm(
        suggestion: InvoiceReconciliationSuggestion,
        selected_combo: dict,
        user,
        category: BankTransactionCategory | None = None,
    ) -> None:
        """
        Confirm a suggestion.

        Two paths depending on the invoice state at confirmation time:

        - Invoice has remaining balance: create Payment + PaymentAllocation +
          BankReconciliation for each selected transaction (original flow).

        - Invoice already fully paid: find the existing unlinked Payment and
          create only the BankReconciliation to link it to the bank transaction.
          No new Payment is created.

        `category`, when given, is assigned to every BankTransaction
        reconciled as part of this confirmation; the invoice decides the
        booking, so it's only a label here.

        Idempotent — skips if suggestion is already confirmed.
        """
        if suggestion.is_confirmed:
            return

        invoice = suggestion.invoice
        invoice_ct = ContentType.objects.get_for_model(invoice)
        invoice_remaining = invoice.amount_remaining_decimal

        for txn_data in selected_combo.get("transactions", []):
            txn = BankTransaction.objects.select_related(
                "bank_account", "company"
            ).get(pk=txn_data["transaction_id"])

            available = Decimal(txn_data["available_amount"])

            # Re-check at confirmation time — another recon may have run in between
            if txn.reconciliation_status == ReconciliationStatus.PARTIAL:
                try:
                    available = txn.reconciliation.unmatched_amount.amount
                except BankReconciliation.DoesNotExist:
                    available = abs(txn.amount.amount)
            elif txn.reconciliation_status == ReconciliationStatus.RECONCILED:
                continue  # Transaction fully used up since suggestion was created

            if available <= Decimal("0.01"):
                continue

            direction = (
                PaymentDirection.INBOUND
                if txn.amount.amount > 0
                else PaymentDirection.OUTBOUND
            )

            if invoice_remaining <= Decimal("0.01"):
                # Invoice is already fully paid — link the bank transaction to
                # the existing Payment instead of creating a new one.
                existing_payment = InvoiceMatcher._find_unlinked_payment(
                    invoice, invoice_ct, available
                )
                if not existing_payment:
                    continue

                BankReconciliation.objects.filter(
                    bank_transaction=txn, is_confirmed=False
                ).delete()

                txn_amount = abs(txn.amount.amount)
                total_already_allocated = (
                    existing_payment.allocations.aggregate(total=Sum("amount_applied"))["total"]
                    or Decimal("0")
                )
                unmatched = max(Decimal("0"), txn_amount - total_already_allocated)
                BankReconciliation.objects.create(
                    bank_transaction=txn,
                    payment=existing_payment,
                    is_confirmed=True,
                    confirmed_at=timezone.now(),
                    reconciled_by=user,
                    method=ReconciliationMethod.AUTO,
                    suggested_allocations=[],
                    unmatched_amount=Money(unmatched, txn.amount_currency),
                )
                txn.reconciliation_status = (
                    ReconciliationStatus.RECONCILED
                    if unmatched <= Decimal("0.01")
                    else ReconciliationStatus.PARTIAL
                )
                update_fields = ["reconciliation_status"]
                if category is not None:
                    txn.category = category
                    update_fields.append("category")
                txn.save(update_fields=update_fields)
                continue

            # Invoice has remaining balance — standard flow: create new Payment.
            amount_to_apply = min(available, invoice_remaining)

            if txn.reconciliation_status == ReconciliationStatus.PARTIAL:
                # Add allocation to the existing confirmed payment on this transaction
                recon = txn.reconciliation
                PaymentAllocation.objects.create(
                    payment=recon.payment,
                    content_type=invoice_ct,
                    object_id=invoice.pk,
                    amount_applied=amount_to_apply,
                )
                new_unmatched = max(
                    Decimal("0"), recon.unmatched_amount.amount - amount_to_apply
                )
                recon.unmatched_amount = Money(new_unmatched, txn.amount_currency)
                recon.save(update_fields=["unmatched_amount"])
            else:
                BankReconciliation.objects.filter(
                    bank_transaction=txn, is_confirmed=False
                ).delete()
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
                PaymentAllocation.objects.create(
                    payment=payment,
                    content_type=invoice_ct,
                    object_id=invoice.pk,
                    amount_applied=amount_to_apply,
                )
                txn_amount = abs(txn.amount.amount)
                unmatched = max(Decimal("0"), txn_amount - amount_to_apply)
                BankReconciliation.objects.create(
                    bank_transaction=txn,
                    payment=payment,
                    is_confirmed=True,
                    confirmed_at=timezone.now(),
                    reconciled_by=user,
                    method=ReconciliationMethod.AUTO,
                    suggested_allocations=[],
                    unmatched_amount=Money(unmatched, txn.amount_currency),
                )

            # Recalculate bank transaction status from live totals
            total_allocated = (
                txn.reconciliation.payment.allocations.aggregate(
                    total=Sum("amount_applied")
                )["total"]
                or Decimal("0")
            )
            txn_amount = abs(txn.amount.amount)
            new_unmatched = max(Decimal("0"), txn_amount - total_allocated)
            txn.reconciliation_status = (
                ReconciliationStatus.RECONCILED
                if new_unmatched <= Decimal("0.01")
                else ReconciliationStatus.PARTIAL
            )
            update_fields = ["reconciliation_status"]
            if category is not None:
                txn.category = category
                update_fields.append("category")
            txn.save(update_fields=update_fields)

            invoice_remaining -= amount_to_apply

        suggestion.is_confirmed = True
        suggestion.save(update_fields=["is_confirmed"])

    @staticmethod
    def _find_unlinked_payment(invoice, invoice_ct, approx_amount: Decimal):
        """
        Find a Payment allocated to this invoice that isn't yet linked to any
        BankTransaction. Used when the invoice is already fully paid.
        Matches by amount within 2% tolerance.
        """
        tolerance = approx_amount * Decimal("0.02")
        allocs = PaymentAllocation.objects.filter(
            content_type=invoice_ct,
            object_id=invoice.pk,
        ).select_related("payment")

        for alloc in allocs:
            payment = alloc.payment
            if BankReconciliation.objects.filter(payment=payment).exists():
                continue  # Already linked to a bank transaction
            if abs(abs(payment.amount.amount) - approx_amount) <= tolerance:
                return payment
        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_candidate_transactions(self) -> list:
        """
        Returns list of (BankTransaction, available_amount) tuples.
        Filters by direction, company, and reconciliation status.
        Applies counterparty IBAN filter when known.
        Transactions dated up to PAYMENT_DATE_TOLERANCE_DAYS before the
        document's date are included, to catch early payments.

        For expense claims (no IBAN to narrow by) the pool is bounded further:
        a time window after the claim, and an amount cap — since all candidate
        amounts are positive, any transaction whose available amount exceeds
        the target (+tolerance) can never be part of a valid combination.
        """
        amount_filter = (
            {"amount__gt": 0}
            if self.direction == PaymentDirection.INBOUND
            else {"amount__lt": 0}
        )

        qs = BankTransaction.objects.filter(
            company=self.invoice.company,
            reconciliation_status__in=[
                ReconciliationStatus.UNRECONCILED,
                ReconciliationStatus.PARTIAL,
            ],
            transaction_date__gte=self._document_date
            - timedelta(days=PAYMENT_DATE_TOLERANCE_DAYS),
            **amount_filter,
        ).select_related("bank_account", "reconciliation")

        amount_cap = None
        if self.invoice_type == "expenseclaim":
            qs = qs.filter(
                transaction_date__lte=self._document_date
                + timedelta(days=CLAIM_MATCH_WINDOW_DAYS)
            )
            amount_cap = self._target_amount * (Decimal("1") + AMOUNT_TOLERANCE_PERCENT)
            # SQL-level prune on the raw amount; partial transactions are kept
            # regardless since their *available* amount (checked below) may be
            # far smaller than the raw transaction amount.
            qs = qs.filter(
                Q(amount__gte=-amount_cap)
                | Q(reconciliation_status=ReconciliationStatus.PARTIAL)
            )

        ibans = self._get_counterparty_ibans()
        if ibans:
            qs = qs.filter(counterparty_iban__in=ibans)
            self.has_counterparty = True

        result = []
        for txn in qs:
            if txn.reconciliation_status == ReconciliationStatus.PARTIAL:
                try:
                    available = txn.reconciliation.unmatched_amount.amount
                except BankReconciliation.DoesNotExist:
                    available = abs(txn.amount.amount)
            else:
                available = abs(txn.amount.amount)

            if available <= Decimal("0"):
                continue
            if amount_cap is not None and available > amount_cap:
                continue
            result.append((txn, available))

        return result

    def _get_counterparty_ibans(self) -> list:
        """Return known IBANs for the invoice's provider or client."""
        if self.invoice_type == "invoice":
            ProviderBankAccount = apps.get_model("materials", "ProviderBankAccount")
            return list(
                ProviderBankAccount.objects.filter(
                    provider=self.invoice.provider,
                    company=self.invoice.company,
                ).values_list("iban", flat=True)
            )
        if self.invoice_type == "expenseclaim":
            return []
        ClientBankAccount = apps.get_model("sales", "ClientBankAccount")
        return list(
            ClientBankAccount.objects.filter(
                client=self.invoice.client,
                company=self.invoice.company,
            ).values_list("iban", flat=True)
        )

    @property
    def _document_total(self):
        """Total amount for the document — total_amount for invoices,
        total_claimed for expense claims (which have no total_amount)."""
        total = getattr(self.invoice, "total_amount", None)
        if total is None:
            total = self.invoice.total_claimed
        return total.amount

    @property
    def _target_amount(self) -> Decimal:
        """Amount the combination search is trying to reach — remaining
        balance, or the full total when the document is already fully paid
        (then we're looking for the transaction that paid it)."""
        remaining = self.invoice.amount_remaining_decimal
        return remaining if remaining > Decimal("0") else self._document_total

    @property
    def _document_date(self):
        """Date to start scanning transactions from — issue date for invoices,
        creation date for expense claims (which have no date_issued)."""
        date_issued = getattr(self.invoice, "date_issued", None)
        if date_issued is not None:
            return date_issued
        return self.invoice.created_at.date()

    def _find_matching_combos(self, candidates: list) -> list:
        """
        Find combinations of (txn, available_amount) that sum to the invoice target.
        - Unpaid/partial invoice: target = amount_remaining (money still owed)
        - Fully paid invoice: target = total_amount (find the transaction that paid it)
        Returns the structured suggested_combinations list.
        """
        target = self._target_amount
        tolerance = target * AMOUNT_TOLERANCE_PERCENT
        max_combo = (
            MAX_COMBO_WITH_COUNTERPARTY
            if self.has_counterparty
            else MAX_COMBO_WITHOUT_COUNTERPARTY
        )

        if not candidates:
            return []

        # --- Tier 1: Exact matches ---
        all_exact = []
        for size in range(1, max_combo + 1):
            if size > len(candidates):
                break
            for combo in itertools_combinations(candidates, size):
                total = sum(amt for _, amt in combo)
                if total == target:
                    all_exact.append(list(combo))
                    if len(all_exact) >= MAX_ALTERNATIVES + 1:
                        break
            if len(all_exact) >= MAX_ALTERNATIVES + 1:
                break

        if all_exact:
            return self._serialize_combos(all_exact, "exact")

        # --- Tier 2: Close matches (within ±2%) ---
        all_close = []
        for size in range(1, max_combo + 1):
            if size > len(candidates):
                break
            for combo in itertools_combinations(candidates, size):
                total = sum(amt for _, amt in combo)
                diff = abs(total - target)
                if diff <= tolerance:
                    all_close.append((list(combo), diff))
                    if len(all_close) >= MAX_ALTERNATIVES + 1:
                        break
            if len(all_close) >= MAX_ALTERNATIVES + 1:
                break

        all_close.sort(key=lambda x: x[1])

        if all_close:
            return self._serialize_combos([c for c, _ in all_close], "close")

        return []

    def _serialize_combos(self, combos: list, confidence: str) -> list:
        """Convert raw combo list to the suggested_combinations JSON structure."""
        result = []
        for idx, combo in enumerate(combos[:MAX_ALTERNATIVES + 1]):
            result.append({
                "transactions": [
                    {
                        "transaction_id": txn.pk,
                        "available_amount": str(available),
                        "is_partial": txn.reconciliation_status == ReconciliationStatus.PARTIAL,
                    }
                    for txn, available in combo
                ],
                "confidence": confidence,
                "is_selected": idx == 0,
            })
        return result
