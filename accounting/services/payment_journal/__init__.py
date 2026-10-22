"""
Payments in the ledger: the engine books the treasury, and one strategy per
kind of allocated document books what each allocation settled.
"""
from accounting.services.payment_journal.registry import AllocationStrategy, CounterpartLine, register, strategy_for

__all__ = ["AllocationStrategy", "CounterpartLine", "register", "strategy_for"]
