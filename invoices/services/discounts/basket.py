from .base import InvoiceDiscountStrategy, entry_level


class BasketDiscountStrategy(InvoiceDiscountStrategy):
    """Standard discount on the overall quote subtotal: carried as part of the
    invoice-level discount, scaled by how much of the quote this invoice
    covers. Also the fallback for legacy breakdown entries with no level."""

    def matches(self, entry):
        from sales.models import DiscountLevel

        return entry_level(entry) == DiscountLevel.BASKET
