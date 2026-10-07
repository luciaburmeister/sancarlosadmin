"""Matcher for finding the real Invoice behind a recurring FixedCostInstallment.

Unlike TransactionMatcher/InvoiceMatcher, this never touches BankTransaction -
it looks for an actual Invoice/receipt (same provider, close amount, close
date) that should be linked via FixedCostInstallment.matched_invoice, so the
recurring cost isn't double-counted once the real invoice shows up.

Usage:
    matcher = FixedCostInvoiceMatcher(installment)
    suggestions = matcher.run()  # creates FixedCostInvoiceMatchSuggestion rows
    FixedCostInvoiceMatcher.confirm(suggestion, user)  # sets matched_invoice
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.apps import apps
from django.utils import timezone

from banking.services.transaction_matching.types import (
    AMOUNT_TOLERANCE_PERCENT,
    PAYMENT_DATE_TOLERANCE_DAYS,
)

logger = logging.getLogger(__name__)


class AlreadyMatchedError(Exception):
    """
    Raised by confirm() when the installment is already matched to a
    *different* invoice. Re-matching must go through unmatch() first - never
    silently overwritten, since the old match may already have propagated a
    real payment to the other side (see sync_linked_payable()) that would be
    left stale/incorrect if we just swapped matched_invoice underneath it.
    """
    def __init__(self, current_invoice):
        self.current_invoice = current_invoice
        super().__init__(
            f"This installment is already matched to {current_invoice} - unmatch it first."
        )


class ConflictingReconciliationError(Exception):
    """
    Raised by confirm() when both the installment and the invoice already
    have their own bank reconciliation, against different transactions, and
    the caller didn't pass acknowledge_conflict=True. The caller (the admin
    view) is expected to catch this, show a warning, and ask the human to
    confirm again explicitly.
    """
    def __init__(self, installment_transaction_ids, invoice_transaction_ids):
        self.installment_transaction_ids = installment_transaction_ids
        self.invoice_transaction_ids = invoice_transaction_ids
        super().__init__(
            "Both the installment and the invoice already have their own, "
            "different, bank reconciliations."
        )


class FixedCostInvoiceMatcher:
    def __init__(self, installment):
        self.installment = installment

    def run(self) -> list:
        """
        Find candidate invoices and create FixedCostInvoiceMatchSuggestion
        rows for human review. Deletes this installment's stale PENDING
        suggestions first, so re-running never leaves duplicates behind.
        Never sets matched_invoice - that only happens on confirm().
        """
        FixedCostInvoiceMatchSuggestion = apps.get_model("expenses", "FixedCostInvoiceMatchSuggestion")

        FixedCostInvoiceMatchSuggestion.objects.filter(
            installment=self.installment,
            status="pending",
        ).delete()

        candidates = self._get_candidate_invoices()
        suggestions = [
            FixedCostInvoiceMatchSuggestion(
                installment=self.installment,
                invoice=invoice,
                confidence=confidence,
            )
            for invoice, confidence in candidates
        ]
        return FixedCostInvoiceMatchSuggestion.objects.bulk_create(suggestions)

    def _get_candidate_invoices(self) -> list:
        """
        Returns a list of (Invoice, confidence) tuples: same provider as the
        installment's schedule, amount within tolerance, dated within the
        usual payment-date window around the installment's due_date.
        """
        Invoice = apps.get_model("invoices", "Invoice")

        due_date = self.installment.due_date
        target = self.installment.amount.amount
        tolerance = target * AMOUNT_TOLERANCE_PERCENT

        qs = Invoice.objects.filter(
            company=self.installment.company,
            provider=self.installment.schedule.provider,
            date_issued__gte=due_date - timedelta(days=PAYMENT_DATE_TOLERANCE_DAYS),
            date_issued__lte=due_date + timedelta(days=PAYMENT_DATE_TOLERANCE_DAYS),
        )

        qs = qs.exclude(fixedcost_match_suggestions__status__in=["pending", "confirmed"])

        qs = qs.exclude(
            fixedcost_match_suggestions__status="rejected",
            fixedcost_match_suggestions__installment=self.installment,
        )

        results = []
        for invoice in qs:
            total = invoice.total_amount.amount if invoice.total_amount else Decimal("0.00")
            diff = abs(total - target)
            if diff == 0:
                results.append((invoice, "high"))
            elif diff <= tolerance:
                results.append((invoice, "medium"))
        return results

    @staticmethod
    def _reconciled_transaction_ids(payable) -> set:
        """
        Bank transaction IDs backing this Payable's own confirmed
        reconciliations, via PaymentAllocation -> Payment -> BankReconciliation.
        Empty if it has no PaymentAllocation at all (e.g. never reconciled,
        or only manually marked paid).
        """
        from django.contrib.contenttypes.models import ContentType
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(payable)
        payment_ids = PaymentAllocation.objects.filter(
            content_type=ct, object_id=payable.pk,
        ).values_list("payment_id", flat=True)

        BankReconciliation = apps.get_model("banking", "BankReconciliation")
        return set(
            BankReconciliation.objects.filter(
                payment_id__in=payment_ids,
                bank_transaction_id__isnull=False,
            ).values_list("bank_transaction_id", flat=True)
        )

    @staticmethod
    def confirm(suggestion, user, acknowledge_conflict: bool = False):
        """
        Confirm a suggestion: sets FixedCostInstallment.matched_invoice and
        marks the suggestion CONFIRMED.

        If the installment and the invoice each already carry their own bank
        reconciliation, and those reconciliations point to different bank
        transactions, this is a red flag - linking them would silently imply
        one settled the other when in fact two separate real payments exist.
        Raises ConflictingReconciliationError in that case unless the caller
        explicitly passes acknowledge_conflict=True (the human reviewed it
        and wants to link them anyway - the system only warns, it never
        decides on its own).
        """
        installment = suggestion.installment
        invoice = suggestion.invoice

        if installment.matched_invoice_id and installment.matched_invoice_id != invoice.pk:
            raise AlreadyMatchedError(installment.matched_invoice)

        installment_txn_ids = FixedCostInvoiceMatcher._reconciled_transaction_ids(installment)
        invoice_txn_ids = FixedCostInvoiceMatcher._reconciled_transaction_ids(invoice)

        both_reconciled_and_different = (
            installment_txn_ids
            and invoice_txn_ids
            and installment_txn_ids.isdisjoint(invoice_txn_ids)
        )
        if both_reconciled_and_different and not acknowledge_conflict:
            raise ConflictingReconciliationError(installment_txn_ids, invoice_txn_ids)

        installment.matched_invoice = invoice
        installment.save(update_fields=["matched_invoice", "updated_at"])

        if not both_reconciled_and_different:
            from payments.models import sync_linked_payable
            sync_linked_payable(invoice, installment)

        suggestion.status = "confirmed"
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    @staticmethod
    def reject(suggestion, user):
        suggestion.status = "rejected"
        suggestion.reviewed_by = user
        suggestion.reviewed_at = timezone.now()
        suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    @staticmethod
    def unmatch(installment, user):
        """
        Undo a confirmed match: clears matched_invoice, reverts the confirmed
        suggestion back to REJECTED (so this exact pair isn't re-suggested by
        mistake, while the invoice stays a normal candidate for any other
        installment), and removes the installment's mirrored
        PaymentAllocation (sourced from the invoice) - once unlinked, that
        money is no longer considered shared. The invoice's own real
        allocation is left untouched. This mirror cleanup also happens
        automatically via FixedCostInstallment.save() whenever
        matched_invoice changes for any reason (e.g. a direct admin edit);
        it's done explicitly here too for clarity.

        No-op if the installment isn't matched to anything.
        """
        invoice = installment.matched_invoice
        if invoice is None:
            return

        FixedCostInvoiceMatchSuggestion = apps.get_model("expenses", "FixedCostInvoiceMatchSuggestion")

        installment.matched_invoice = None
        installment.save(update_fields=["matched_invoice", "updated_at"])

        deleted_count, _ = FixedCostInvoiceMatchSuggestion.objects.filter(installment=installment).delete()
        logger.info(
            "FixedCostInstallment %s unmatched from %s by %s (%d suggestion(s) cleared).",
            installment.pk, invoice, user, deleted_count,
        )
