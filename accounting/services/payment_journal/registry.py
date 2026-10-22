"""
What a payment's allocation books, per kind of allocated document.

Each app declares the strategies for its own documents in
``<app>/ledger_strategies.py``, registered with ``@register``;
``AccountingConfig.ready()`` imports them all, the way the admin finds
``admin.py`` modules. A payment allocated to a document with no strategy
isn't journalized at all, never half of it.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import autodiscover_modules

_strategies = {}


@dataclass(frozen=True)
class CounterpartLine:
    """One line of what an allocation settled; the bank's side is booked once, by the engine."""

    account: object  # accounting.CompanyAccount
    description: str
    amount: Decimal


class AllocationStrategy:
    """
    Books what one allocation settled: the counterpart of the money.

    ``lines`` returns the accounts the allocation goes to, with amounts
    adding up to ``allocation.amount_applied`` (same sign). The engine puts
    them on the other side of the bank's line, so a strategy never decides
    debit or credit. ``ledger`` is the PaymentJournalService: it looks up
    and opens client, provider and role accounts.
    """

    model = None

    def lines(self, allocation, document, ledger):
        raise NotImplementedError


def register(strategy_class):
    """Class decorator: ``strategy_class`` books allocations to its ``model``."""
    key = strategy_class.model._meta.label_lower
    if key in _strategies:
        raise ImproperlyConfigured(f"{key} already has a ledger strategy: {type(_strategies[key]).__name__}")
    _strategies[key] = strategy_class()
    return strategy_class


def strategy_for(model):
    return _strategies.get(model._meta.label_lower)


def autodiscover():
    autodiscover_modules("ledger_strategies")
