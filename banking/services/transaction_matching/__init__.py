"""Transaction matching package for bank reconciliation.

Public API:
    - TransactionMatcher          (class — main orchestrator)
    - TransactionMatcher.run()    → BankReconciliation  (suggestion)
    - TransactionMatcher.confirm() → BankReconciliation (human-confirmed)
    - MatchResult                 (dataclass)
"""
from banking.services.transaction_matching.invoice_matcher import InvoiceMatcher
from banking.services.transaction_matching.matcher import TransactionMatcher
from banking.services.transaction_matching.types import MatchResult

__all__ = [
    "InvoiceMatcher",
    "TransactionMatcher",
    "MatchResult",
]
