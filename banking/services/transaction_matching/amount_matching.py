"""Amount matching logic for bank transaction reconciliation.

Uses a 2-tier strategy:
1. Exact match — remaining amount equals transaction amount exactly
2. Close match — remaining amount within ±2% (capped at €10)

Performance notes:
- Remaining amounts are pre-fetched in a single pass (avoids N+1 queries).
- Candidates are capped at MAX_CANDIDATES to prevent combinatorial explosion.
- Combinations are only tried within the same counterparty group.
- Combinations are only tried up to size 3.
"""
from collections import defaultdict
from decimal import Decimal
from itertools import combinations

from banking.models import BankTransaction
from banking.services.transaction_matching.types import (
    AMOUNT_TOLERANCE_PERCENT,
    MatchResult,
)

# Maximum number of candidates to consider per counterparty group.
MAX_CANDIDATES_PER_GROUP = 50
MAX_COMBINATION_SIZE = 3


def match_amount(bank_transaction: BankTransaction, candidates: list) -> MatchResult:
    """
    Tries to find a combination of candidates whose remaining amounts
    match the transaction amount.

    Candidates must implement the ``Payable`` mixin (i.e. expose
    ``amount_remaining_decimal`` and ``counterparty_key``).

    Combinations are only tried within the same counterparty — it makes
    no sense to combine an invoice from provider A with one from provider B.
    """
    target = abs(bank_transaction.amount.amount)
    tolerance = target * AMOUNT_TOLERANCE_PERCENT

    if not candidates:
        return MatchResult(unmatched_amount=target)

    # Pre-fetch all remaining amounts in one pass to avoid N+1 queries.
    # Each entry is (doc, remaining_decimal, counterparty_key).
    entries = [
        (doc, doc.amount_remaining_decimal, doc.counterparty_key)
        for doc in candidates
    ]

    # Drop zero-remaining documents — they can't contribute to a match
    entries = [(doc, amt, key) for doc, amt, key in entries if amt > 0]

    # --- Tier 1: Exact single match (across all candidates) ---
    for doc, remaining, _ in entries:
        if remaining == target:
            return MatchResult(documents=[doc], confidence="exact")

    # --- Group by counterparty for combination matching ---
    groups = defaultdict(list)
    for doc, remaining, key in entries:
        groups[key].append((doc, remaining))

    # Sort each group by closeness to target and cap size
    for key in groups:
        groups[key].sort(key=lambda pair: abs(pair[1] - target))
        groups[key] = groups[key][:MAX_CANDIDATES_PER_GROUP]

    # --- Tier 1 continued: Exact combination match (within same counterparty) ---
    for group in groups.values():
        for size in range(2, MAX_COMBINATION_SIZE + 1):
            for combo in combinations(group, size):
                total = sum(amt for _, amt in combo)
                if total == target:
                    return MatchResult(
                        documents=[doc for doc, _ in combo],
                        confidence="exact",
                    )

    # --- Tier 2: Close single match (pick the closest one) ---
    best_pair = None
    best_diff = None
    for doc, remaining, _ in entries:
        diff = abs(remaining - target)
        if diff <= tolerance:
            if best_diff is None or diff < best_diff:
                best_pair = (doc, remaining)
                best_diff = diff

    if best_pair is not None:
        doc, remaining = best_pair
        return MatchResult(
            documents=[doc],
            unmatched_amount=abs(remaining - target),
            confidence="close",
        )

    # --- Tier 2 continued: Close combination match (within same counterparty) ---
    for group in groups.values():
        for size in range(2, MAX_COMBINATION_SIZE + 1):
            for combo in combinations(group, size):
                total = sum(amt for _, amt in combo)
                diff = abs(total - target)
                if diff <= tolerance:
                    return MatchResult(
                        documents=[doc for doc, _ in combo],
                        unmatched_amount=diff,
                        confidence="close",
                    )

    # No match found
    return MatchResult(unmatched_amount=target)
