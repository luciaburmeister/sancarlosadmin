"""Amount matching logic for bank transaction reconciliation.

Uses a 2-tier strategy:
1. Exact match — remaining amount equals transaction amount exactly
2. Close match — remaining amount within ±2%

Strategy-dependent combination depth and ordering:
- With counterparty: up to 10 invoices, largest combo first (clear more debt)
- Without counterparty: up to 3 invoices, smallest combo first (safer)

All valid combinations are collected and stored as alternatives so the
user can pick the correct one without recomputing.
"""
from collections import defaultdict
from decimal import Decimal
from itertools import combinations

from banking.models import BankTransaction
from banking.services.transaction_matching.types import (
    AMOUNT_TOLERANCE_PERCENT,
    MatchResult,
)

# When a counterparty was identified, the candidate pool is small
# so we can afford deeper combinations.
MAX_COMBO_WITH_COUNTERPARTY = 10
MAX_COMBO_WITHOUT_COUNTERPARTY = 3

MAX_CANDIDATES_PER_GROUP = 50

# Cap on total alternatives to keep storage reasonable
MAX_ALTERNATIVES = 20


def _combo_sizes(max_combo: int, largest_first: bool) -> range:
    """Return the range of combination sizes to try, in the right order."""
    if largest_first:
        return range(max_combo, 1, -1)
    return range(2, max_combo + 1)


def match_amount(
    bank_transaction: BankTransaction,
    candidates: list,
    has_counterparty: bool = False,
) -> MatchResult:
    """
    Tries to find a combination of candidates whose remaining amounts
    match the transaction amount.

    Collects ALL valid combinations and returns the best as the primary
    match, with the rest stored in ``alternatives``.
    """
    target = abs(bank_transaction.amount.amount)
    tolerance = target * AMOUNT_TOLERANCE_PERCENT
    max_combo = (
        MAX_COMBO_WITH_COUNTERPARTY if has_counterparty
        else MAX_COMBO_WITHOUT_COUNTERPARTY
    )
    sizes = _combo_sizes(max_combo, largest_first=has_counterparty)

    if not candidates:
        return MatchResult(unmatched_amount=target)

    # Pre-fetch all remaining amounts in one pass to avoid N+1 queries.
    entries = [
        (doc, doc.amount_remaining_decimal, doc.counterparty_key)
        for doc in candidates
    ]

    # Drop zero-remaining documents
    entries = [(doc, amt, key) for doc, amt, key in entries if amt > 0]

    # --- Tier 1: Exact single match (across all candidates) ---
    exact_singles = []
    for doc, remaining, _ in entries:
        if remaining == target:
            exact_singles.append([doc])

    if exact_singles:
        return MatchResult(
            documents=exact_singles[0],
            confidence="exact",
            alternatives=exact_singles[1:MAX_ALTERNATIVES],
        )

    # --- Group by counterparty for combination matching ---
    groups = defaultdict(list)
    for doc, remaining, key in entries:
        groups[key].append((doc, remaining))

    for key in groups:
        groups[key].sort(key=lambda pair: abs(pair[1] - target))
        groups[key] = groups[key][:MAX_CANDIDATES_PER_GROUP]

    # --- Tier 1 continued: Exact combination matches ---
    exact_combos = []
    for group in groups.values():
        for size in sizes:
            if size > len(group):
                continue
            for combo in combinations(group, size):
                total = sum(amt for _, amt in combo)
                if total == target:
                    exact_combos.append([doc for doc, _ in combo])
                    if len(exact_combos) >= MAX_ALTERNATIVES:
                        break
            if len(exact_combos) >= MAX_ALTERNATIVES:
                break

    # exact_singles is always empty here (handled above), so this is really
    # just exact_combos — kept as-is to minimize the diff against the
    # pre-existing combination logic below.
    if exact_combos:
        best = exact_combos[0]
        alternatives = exact_combos[1:MAX_ALTERNATIVES]
        return MatchResult(
            documents=best,
            confidence="exact",
            alternatives=alternatives,
        )

    # --- Tier 2: Close single match (pick the closest one) ---
    close_singles = []
    for doc, remaining, _ in entries:
        diff = abs(remaining - target)
        if diff <= tolerance:
            close_singles.append(([doc], diff))

    # Sort by smallest difference
    close_singles.sort(key=lambda x: x[1])

    # --- Tier 2 continued: Close combination matches ---
    close_combos = []
    for group in groups.values():
        for size in sizes:
            if size > len(group):
                continue
            for combo in combinations(group, size):
                total = sum(amt for _, amt in combo)
                diff = abs(total - target)
                if diff <= tolerance:
                    close_combos.append(([doc for doc, _ in combo], diff))
                    if len(close_combos) >= MAX_ALTERNATIVES:
                        break
            if len(close_combos) >= MAX_ALTERNATIVES:
                break

    all_close = close_singles + close_combos
    all_close.sort(key=lambda x: x[1])

    if all_close:
        best_docs, best_diff = all_close[0]
        alternatives = [docs for docs, _ in all_close[1:MAX_ALTERNATIVES]]
        return MatchResult(
            documents=best_docs,
            unmatched_amount=best_diff,
            confidence="close",
            alternatives=alternatives,
        )

    # No match found
    return MatchResult(unmatched_amount=target)
