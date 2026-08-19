"""Shared strategy interface for ``DiscountRule.level`` handling.

Each ``DiscountLevel`` choice gets its own ``DiscountLevelHandler`` subclass
(one per file in this package) describing:
- which base amount its rules apply against (``resolve_base``)
- which recalculation phase it runs in:
    - ``PRE_SUBTOTAL`` mutates records (e.g. line item discounts) before
      ``Quote._calculate_subtotal()`` runs, so its effect is already baked
      into ``item_total``/``config_subtotal`` by the time ``POST_SUBTOTAL``
      handlers see them.
    - ``POST_SUBTOTAL`` (the default) computes an amount to subtract from
      the quote's overall subtotal.

To add a new level: add a ``DiscountLevel`` choice in ``sales.models``, add a
new file here with a ``DiscountLevelHandler`` subclass implementing
``resolve_base`` (and, only if its behaviour differs from "subtract the
computed amount", overriding ``run``), and list it in
``registry.DiscountLevelRegistry.handler_classes``. No changes to
``Quote.recalc_totals`` are needed.
"""

PRE_SUBTOTAL = "pre_subtotal"
POST_SUBTOTAL = "post_subtotal"


class DiscountLevelHandler:
    """Base strategy for a single DiscountLevel."""

    level = None
    phase = POST_SUBTOTAL

    def resolve_base(self, quote, totals):
        """Return the Decimal base amount this level's rules apply against.

        ``totals`` is a dict with ``item_total``, ``config_subtotal`` and
        ``raw_subtotal`` Decimals (``None`` for PRE_SUBTOTAL handlers, which
        run before these are computed).
        """
        raise NotImplementedError

    def run(self, quote, totals):
        """Compute this level's discount. Returns (amount_to_subtract, breakdown)."""
        from sales.services.discount_service import DiscountService

        rules = quote._discount_rules_for_level(self.level)
        base = self.resolve_base(quote, totals)
        context = quote._build_discount_context(base)
        amount, breakdown = DiscountService.compute(base, rules, context)

        for entry in breakdown:
            entry["level"] = str(self.level)

        return amount, breakdown
