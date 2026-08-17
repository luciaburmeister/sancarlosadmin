"""Shared types and constants for transaction matching."""
from dataclasses import dataclass, field
from decimal import Decimal


# Tolerance for "close" amount matching (2%)
AMOUNT_TOLERANCE_PERCENT = Decimal("0.02")

# A payment can be dated up to this many days before the invoice/document
# date and still be considered a candidate match (e.g. early payments).
PAYMENT_DATE_TOLERANCE_DAYS = 15


@dataclass
class MatchResult:
    """Result of the amount-matching step."""
    documents: list = field(default_factory=list)
    unmatched_amount: Decimal = Decimal("0.00")
    confidence: str = "none"  # "exact", "close", or "none"
    alternatives: list = field(default_factory=list)  # list of doc-lists

    @property
    def has_match(self) -> bool:
        return len(self.documents) > 0

