from .base import (
    InvoiceDiscountStrategy,
    ALL_KINDS,
    LINE_ITEM_KINDS,
    entry_level,
    line_items_base,
)


class FinancingDiscountStrategy(InvoiceDiscountStrategy):
    """Financing discounts join the document-level discount with their own
    breakdown entry. The entry's base (and therefore partial-document
    scaling) follows its configured level."""

    def matches(self, entry):
        from sales.models import DiscountKind, DiscountLevel

        return (
            entry.get("kind") == DiscountKind.FINANCING
            and entry_level(entry) != DiscountLevel.LINE_ITEM
        )

    def eligible_kinds(self, entry):
        from sales.models import DiscountLevel

        if entry_level(entry) == DiscountLevel.LINE_ITEMS_BASKET:
            return LINE_ITEM_KINDS
        return ALL_KINDS

    def quote_base(self, quote, entry):
        from sales.models import DiscountLevel
        from decimal import Decimal

        if entry_level(entry) == DiscountLevel.LINE_ITEMS_BASKET:
            return line_items_base(quote)
        return Decimal(str(quote.subtotal_price.amount))
