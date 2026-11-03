"""What a payment allocated to a promissory note's maturity books: its own bridge account (401/431)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from accounting.services.payment_journal import AllocationStrategy, CounterpartLine, Ledger, register
from payments.models import PromissoryNote

if TYPE_CHECKING:
    from payments.models import PaymentAllocation


@register
class PromissoryNoteStrategy(AllocationStrategy[PromissoryNote]):
    model = PromissoryNote

    def lines(
        self, allocation: PaymentAllocation, document: PromissoryNote, ledger: Ledger
    ) -> list[CounterpartLine]:
        counterparty = document.provider.name if document.provider_id else document.client.name
        return [CounterpartLine(
            ledger.notes_account(issued=bool(document.provider_id)),
            f"Pagaré {document.number} - {counterparty}",
            allocation.amount_applied.amount,
        )]
