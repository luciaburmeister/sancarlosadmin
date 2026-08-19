from decimal import Decimal

from sales.models import DiscountLevel
from .base import DiscountLevelHandler, PRE_SUBTOTAL


class LineItemLevelHandler(DiscountLevelHandler):
    """Writes an effective discount % onto each QuoteInventoryItem instead of
    subtracting an amount from the subtotal — by the time POST_SUBTOTAL
    handlers run, the discount is already baked into item_total via each
    item's recalculated line_total."""

    level = DiscountLevel.LINE_ITEM
    phase = PRE_SUBTOTAL

    def resolve_base(self, quote, totals=None):
        item_total = sum(
            (item.line_total.amount for item in quote.items.all()),
            Decimal("0.00"),
        )
        inv_total = sum(
            (item.unit_price.amount * item.quantity for item in quote.inventory_items.all()),
            Decimal("0.00"),
        )
        config_subtotal = sum(
            (c.subtotal_price.amount for c in quote.configurations.all()),
            Decimal("0.00"),
        )
        return item_total + inv_total + config_subtotal

    def run(self, quote, totals=None):
        from sales.services.discount_service import DiscountService

        rules = quote._discount_rules_for_level(self.level)
        if not rules:
            return Decimal("0.00"), []

        # Base uses undiscounted item prices so condition thresholds aren't
        # affected by the discount being calculated.
        base = self.resolve_base(quote)
        context = quote._build_discount_context(base)

        # Effective percentage: run the rules against a 100-unit base so the
        # returned discount amount equals the combined percentage directly.
        effective_pct, breakdown = DiscountService.compute_effective_pct(rules, context)

        for inv_item in quote.inventory_items.all():
            inv_item.discount = effective_pct
            inv_item._skip_quote_recalc = True
            inv_item.save()

        for entry in breakdown:
            entry["level"] = str(self.level)

        # Already reflected in item_total; nothing left to subtract.
        return Decimal("0.00"), breakdown
