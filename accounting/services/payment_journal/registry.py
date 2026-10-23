"""
What a payment's allocation books, per kind of allocated document.

Each app declares the strategies for its own documents in
``<app>/ledger_strategies.py``, registered with ``@register``;
``AccountingConfig.ready()`` imports them all, the way the admin finds
``admin.py`` modules. A payment allocated to a document with no strategy
isn't journalized at all, never half of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model
from django.utils.module_loading import autodiscover_modules

if TYPE_CHECKING:
    from accounting.models import AccountRole, CompanyAccount
    from company.models import Company
    from materials.models import Provider
    from accounting.models import JournalType
    from payments.models import Payment, PaymentAllocation
    from sales.models import Client

_strategies: dict[str, AllocationStrategy] = {}
_treasury_strategies: dict[str, TreasuryStrategy] = {}


@dataclass(frozen=True)
class CounterpartLine:
    """One line of what an allocation settled; the bank's side is booked once, by the engine."""

    account: CompanyAccount
    description: str
    amount: Decimal


class Ledger(Protocol):
    """
    What a strategy may ask the ledger for: all it gets is the company and
    its accounts. PaymentJournalService is the implementation; the account
    getters open a client's or provider's account on first use, and raise
    MissingAccount when no account has the role.
    """

    company: Company

    def client_account(self, client: Client) -> CompanyAccount: ...

    def provider_account(self, provider: Provider) -> CompanyAccount: ...

    def role_account(self, role: AccountRole) -> CompanyAccount: ...


class AllocationStrategy[DocumentT: Model]:
    """
    Books what one allocation settled: the counterpart of the money.

    ``lines`` returns the accounts the allocation goes to, with amounts
    adding up to ``allocation.amount_applied`` (same sign). The engine puts
    them on the other side of the bank's line, so a strategy never decides
    debit or credit. ``ledger`` (see Ledger) looks up and opens the client,
    provider and role accounts the lines go to.

    ``DocumentT`` is the allocated document's model, the one set as
    ``model``: ``document`` is ``allocation.related_object``, already
    loaded and of that type.
    """

    model: type[DocumentT] | None = None

    def lines(
        self, allocation: PaymentAllocation, document: DocumentT, ledger: Ledger
    ) -> list[CounterpartLine]:
        raise NotImplementedError


def register[S: type[AllocationStrategy]](strategy_class: S) -> S:
    """Class decorator: ``strategy_class`` books allocations to its ``model``."""
    key = strategy_class.model._meta.label_lower
    if key in _strategies:
        raise ImproperlyConfigured(f"{key} already has a ledger strategy: {type(_strategies[key]).__name__}")
    _strategies[key] = strategy_class()
    return strategy_class


def strategy_for(model: type[Model]) -> AllocationStrategy | None:
    return _strategies.get(model._meta.label_lower)


class TreasuryStrategy:
    """
    The money side of payments whose ``method`` moves no money through a
    bank account or the till (the engine books those itself): the account
    the payment's whole amount is booked to, opposite its allocations, and
    the journal it goes in, ``(JournalType, name, code)``.
    """

    method: str | None = None
    journal: tuple[JournalType, str, str] | None = None

    def account(self, payment: Payment, ledger: Ledger) -> CompanyAccount:
        raise NotImplementedError


def register_treasury[S: type[TreasuryStrategy]](strategy_class: S) -> S:
    """Class decorator: ``strategy_class`` books the treasury of payments of its ``method``."""
    key = str(strategy_class.method)
    if key in _treasury_strategies:
        raise ImproperlyConfigured(
            f"Payment method {key} already has a treasury strategy: {type(_treasury_strategies[key]).__name__}"
        )
    _treasury_strategies[key] = strategy_class()
    return strategy_class


def treasury_strategy_for(method: str) -> TreasuryStrategy | None:
    return _treasury_strategies.get(str(method))


def autodiscover() -> None:
    autodiscover_modules("ledger_strategies")
