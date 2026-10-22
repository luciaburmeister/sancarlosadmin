"""
The strategy for documents no ``ledger_strategies.py`` covers: booked to an
account chosen in the admin instead of in code.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Model

from accounting.models import AllocationAccountRule
from accounting.services.payment_journal.registry import (
    AllocationStrategy, CounterpartLine, Ledger,
)

if TYPE_CHECKING:
    from accounting.models import CompanyAccount
    from company.models import Company
    from payments.models import PaymentAllocation


class ConfiguredAccountStrategy(AllocationStrategy[Model]):
    """
    The whole allocation on one account: the one set on the allocation, or
    else the company's rule for that kind of document. Only used when the
    document type has no strategy in code.
    """

    def account_for(
        self, allocation: PaymentAllocation, company: Company
    ) -> CompanyAccount | None:
        if allocation.account_id:
            return allocation.account
        rule = (
            AllocationAccountRule.objects.select_related("account")
            .filter(company=company, content_type_id=allocation.content_type_id)
            .first()
        )
        return rule.account if rule else None

    def lines(
        self, allocation: PaymentAllocation, document: Model, ledger: Ledger
    ) -> list[CounterpartLine]:
        return [CounterpartLine(
            self.account_for(allocation, ledger.company),
            f"{allocation.content_type.model_class()._meta.verbose_name} {document}"[:1024],
            allocation.amount_applied.amount,
        )]


configured_strategy = ConfiguredAccountStrategy()
