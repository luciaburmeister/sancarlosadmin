from .base import InvoiceDiscountStrategy, PASS_THROUGH


class LineItemPassThroughStrategy(InvoiceDiscountStrategy):
    """Line-item discounts are written onto each QuoteInventoryItem as an
    effective ``discount`` % by the quote's recalculation, so they are already
    reflected in the source line's price. Invoice builders carry them by
    copying ``inv_item.discount`` into the invoice line's
    ``discount_percentage`` — nothing to add at invoice level.

    Note: line_item breakdown entries store the effective *percentage* in
    ``amount`` (the cascade runs against a 100-unit base; only the
    ``accumulated`` condition variable is scaled to money), never money — they
    must not be summed with the other entries.
    """

    materialization = PASS_THROUGH

    def matches(self, entry):
        from sales.models import DiscountLevel

        return entry.get("level") == DiscountLevel.LINE_ITEM
