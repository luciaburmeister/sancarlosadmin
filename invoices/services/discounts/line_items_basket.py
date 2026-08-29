from .base import InvoiceDiscountStrategy, LINE_ITEM_KINDS, line_items_base


class LineItemsBasketDiscountStrategy(InvoiceDiscountStrategy):
    """Standard discount on the line-items subtotal only: carried as part of
    the invoice-level discount, scaled by how much of the quote's *line items*
    (never its configurations) this invoice covers."""

    def matches(self, entry):
        from sales.models import DiscountLevel

        return entry.get("level") == DiscountLevel.LINE_ITEMS_BASKET

    def eligible_kinds(self, entry):
        return LINE_ITEM_KINDS

    def quote_base(self, quote, entry):
        return line_items_base(quote)
