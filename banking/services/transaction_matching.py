from decimal import Decimal
from itertools import combinations
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from djmoney.money import Money
from banking.models import (
    BankReconciliation,
    BankTransaction,
    ReconciliationMethod,
    ReconciliationStatus,
)
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentCreatedBy,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)


def run_matcher(bank_transaction: BankTransaction) -> BankReconciliation:
    """
    Called immediately after a BankTransaction is imported.
    Runs the matching logic and creates a BankReconciliation SUGGESTION.
    Never creates a Payment, that happens only when a human confirms.
    """
    direction = _get_direction(bank_transaction)
    counterparty = _find_counterparty(bank_transaction, direction)
    candidates = _get_candidate_documents(bank_transaction, direction, counterparty)
    matched, unmatched_amount = _match_amount(bank_transaction, candidates)

    suggested_allocations = [
        {
            "content_type": ContentType.objects.get_for_model(doc).model,
            "object_id": doc.pk,
        }
        for doc in matched
    ]

    reconciliation = BankReconciliation.objects.create(
        bank_transaction=bank_transaction,
        payment=None,
        is_confirmed=False,
        method=ReconciliationMethod.AUTO,
        suggested_allocations=suggested_allocations,
        unmatched_amount=Money(unmatched_amount, bank_transaction.amount_currency),
    )

    return reconciliation


@transaction.atomic
def confirm_reconciliation(
    reconciliation: BankReconciliation,
    confirmed_allocations: list,
    user,
) -> BankReconciliation:
    """
    Called when a human confirms or modifies the matcher's suggestion.

    confirmed_allocations is a list of dicts:
        [{"content_type": "invoice", "object_id": 5}, ...]

    This function:
    1. Determines if the human changed the suggestion (sets method accordingly)
    2. Creates the Payment
    3. Creates PaymentAllocations
    4. Links the Payment to the BankReconciliation
    5. Updates statuses
    """
    # Step 1 — Did the human change the suggestion?
    method = _determine_method(reconciliation.suggested_allocations, confirmed_allocations)

    # Step 2 — Create the Payment
    txn = reconciliation.bank_transaction
    direction = _get_direction(txn)

    payment = Payment.objects.create(
        company=txn.company,
        payment_date=txn.transaction_date,
        amount=abs(txn.amount.amount),
        amount_currency=txn.amount_currency,
        direction=direction,
        method=PaymentMethod.TRANSFER,
        status=PaymentStatus.DRAFT,
        created_by=PaymentCreatedBy.AUTO,
        bank_account=txn.bank_account,
    )

    # Step 3 — Create PaymentAllocations
    for allocation in confirmed_allocations:
        ct = ContentType.objects.get(model=allocation["content_type"])
        doc = ct.get_object_for_this_type(pk=allocation["object_id"])
        remaining = _get_remaining_amount(doc)
        PaymentAllocation.objects.create(
            payment=payment,
            content_type=ct,
            object_id=allocation["object_id"],
            amount_applied=remaining,
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


# ---------------------------------------------------------------------------
# Private helper functions
# ---------------------------------------------------------------------------

def _get_direction(transaction: BankTransaction) -> str:
    """
    Determines payment direction from the transaction amount.
    Positive amount = money came in = inbound.
    Negative amount = money went out = outbound.
    Zero is treated as outbound (edge case, unlikely in practice).
    """
    if transaction.amount.amount > 0:
        return PaymentDirection.INBOUND
    return PaymentDirection.OUTBOUND


def _find_counterparty(transaction: BankTransaction, direction: str):
    """
    Tries to identify who the counterparty is by matching
    counterparty_iban against known Provider or Client bank accounts.
    Returns the Provider or Client object if found, None otherwise.
    """
    iban = transaction.counterparty_iban
    if not iban:
        return None

    if direction == PaymentDirection.OUTBOUND:
        ProviderBankAccount = apps.get_model("materials", "ProviderBankAccount")
        match = (
            ProviderBankAccount.objects
            .filter(iban=iban, company=transaction.company)
            .select_related("provider")
            .first()
        )
        return match.provider if match else None

    else:
        ClientBankAccount = apps.get_model("sales", "ClientBankAccount")
        match = (
            ClientBankAccount.objects
            .filter(iban=iban, company=transaction.company)
            .select_related("client")
            .first()
        )
        return match.client if match else None


def _get_candidate_documents(
    transaction: BankTransaction,
    direction: str,
    counterparty,
) -> list:
    """
    Fetches unpaid or partially paid documents that could match this transaction.
    If a counterparty was identified, filters to that counterparty's documents only.
    If no counterparty, returns all unpaid documents for the company in that direction.
    """
    Invoice = apps.get_model("invoices", "Invoice")
    SalesInvoice = apps.get_model("invoices", "SalesInvoice")
    # We would add more document types here in the future (expenses, payroll, etc.)

    if direction == PaymentDirection.OUTBOUND:
        qs = Invoice.objects.filter(
            company=transaction.company,
            payment_status__in=["unpaid", "partial"],
        )
        if counterparty:
            qs = qs.filter(provider=counterparty)
        return list(qs)

    else:
        qs = SalesInvoice.objects.filter(
            company=transaction.company,
            payment_status__in=["unpaid", "partial"],
        )
        if counterparty:
            qs = qs.filter(client=counterparty)
        return list(qs)


def _match_amount(bank_transaction: BankTransaction, candidates: list) -> tuple:
    """
    Tries to find a combination of candidates whose remaining amounts
    sum exactly to the transaction amount.

    Returns a tuple of:
        - matched: list of matched document objects
        - unmatched_amount: Decimal of what couldn't be matched (0 if full match)

    Search is capped at combinations of 3 documents to keep it performant.
    """
    MAX_COMBINATION_SIZE = 3
    target = abs(bank_transaction.amount.amount)

    if not candidates:
        return [], target

    # Try single match first
    for doc in candidates:
        remaining = _get_remaining_amount_decimal(doc)
        if remaining == target:
            return [doc], Decimal("0.00")

    # Try combinations of 2 and 3
    for size in range(2, MAX_COMBINATION_SIZE + 1):
        for combo in combinations(candidates, size):
            total = sum(_get_remaining_amount_decimal(doc) for doc in combo)
            if total == target:
                return list(combo), Decimal("0.00")

    # No exact match found
    return [], target


def _get_remaining_amount(doc) -> Decimal:
    """
    Returns amount_remaining for any document type as a plain Decimal.
    Works for Invoice, SalesInvoice, and any future document type
    that implements an amount_remaining property.
    """
    remaining = doc.amount_remaining
    if hasattr(remaining, "amount"):
        return Decimal(str(remaining.amount))
    return Decimal(str(remaining))


# Alias used inside _match_amount for clarity
_get_remaining_amount_decimal = _get_remaining_amount


def _determine_method(suggested_allocations: list, confirmed_allocations: list) -> str:
    """
    Compares what the matcher suggested against what the human confirmed.
    If they match exactly → AUTO (human agreed with the matcher).
    If they differ → MANUAL (human changed the suggestion).
    If there was no suggestion → MANUAL (human created from scratch).
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