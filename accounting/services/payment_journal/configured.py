"""
The strategy for documents no ``ledger_strategies.py`` covers: booked to an
account chosen in the admin instead of in code.
"""
from accounting.models import AllocationAccountRule
from accounting.services.payment_journal.registry import AllocationStrategy, CounterpartLine


class ConfiguredAccountStrategy(AllocationStrategy):
    """
    The whole allocation on one account: the one set on the allocation, or
    else the company's rule for that kind of document. Only used when the
    document type has no strategy in code.
    """

    def account_for(self, allocation, company):
        if allocation.account_id:
            return allocation.account
        rule = (
            AllocationAccountRule.objects.select_related("account")
            .filter(company=company, content_type_id=allocation.content_type_id)
            .first()
        )
        return rule.account if rule else None

    def lines(self, allocation, document, ledger):
        return [CounterpartLine(
            self.account_for(allocation, ledger.company),
            f"{allocation.content_type.model_class()._meta.verbose_name} {document}"[:1024],
            allocation.amount_applied.amount,
        )]


configured_strategy = ConfiguredAccountStrategy()
