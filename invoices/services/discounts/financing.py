from django.utils.translation import gettext as _

from .base import (
    InvoiceDiscountStrategy,
    DISCOUNT_LINE,
    ALL_KINDS,
    LINE_ITEM_KINDS,
    entry_level,
    line_items_base,
)


class FinancingDiscountStrategy(InvoiceDiscountStrategy):
    """Financing discounts are not item-level discounts: they show up on the
    invoice as their own negative line, so the client sees the financing
    amount called out. The entry's base (and therefore partial-invoice
    scaling) still follows its configured level."""

    materialization = DISCOUNT_LINE

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

    def line_description(self, quote, entry):
        rule_name = entry.get("rule_name") or ""
        label = _("Financing discount")
        return f"{label} — {rule_name}" if rule_name else str(label)
