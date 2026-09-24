from dataclasses import dataclass, field
from decimal import Decimal

from .base import INVOICE_LEVEL, PASS_THROUGH, source_kind
from .registry import InvoiceDiscountStrategyRegistry

TWO_PLACES = Decimal("0.01")


@dataclass
class SourceLine:
    """One invoice line traced back to its source quote."""

    source_id: str
    line_total: Decimal
    quote_id: int | None
    kind: str | None = None

    def __post_init__(self):
        if self.kind is None:
            self.kind = source_kind(self.source_id)


@dataclass
class DiscountResolution:
    """How the source quotes' discounts land on one document.

    - ``discount_amount``/``breakdown``: document-level discount (standard
      and financing basket rules), to store on ``discount_amount`` /
      ``discount_breakdown`` of the invoice or delivery order.
    - ``taxable_fraction(quote_id, kind)``: fraction by which a source line's
      total must be reduced to get its taxable base, per quote and line kind.
    """

    discount_amount: Decimal = Decimal("0.00")
    breakdown: list = field(default_factory=list)
    _fractions: dict = field(default_factory=dict)

    def taxable_fraction(self, quote_id, kind):
        frac = self._fractions.get((quote_id, kind), Decimal("0"))
        return min(frac, Decimal("1"))


class QuoteDiscountResolver:
    """Carries source quotes' discounts onto a document via the registered
    strategies, scaling each discount by how much of its own base this
    document covers — so a quote's discount never bleeds into lines invoiced
    from other quotes, and a line-items-only discount never scales with
    configuration lines."""

    def __init__(self, registry=None):
        self.registry = registry or InvoiceDiscountStrategyRegistry()

    def resolve(self, quotes, lines) -> DiscountResolution:
        """
        Args:
            quotes: iterable of Quote instances the lines may come from
            lines: iterable of SourceLine
        """
        resolution = DiscountResolution()

        invoiced: dict[int, dict[str, Decimal]] = {}  # quote_id → kind → total
        for line in lines:
            if line.quote_id is None or line.kind is None:
                continue
            per_kind = invoiced.setdefault(line.quote_id, {})
            per_kind[line.kind] = per_kind.get(line.kind, Decimal("0")) + line.line_total

        for quote in quotes:
            if not (quote.discount_amount and quote.discount_amount.amount > 0):
                continue
            per_kind = invoiced.get(quote.id)
            if not per_kind:
                continue
            self._resolve_quote(quote, per_kind, resolution)

        return resolution

    def _resolve_quote(self, quote, per_kind, resolution):
        claimed = []  # (entry, strategy) pairs carrying money
        for entry in (quote.discount_breakdown or []):
            strategy = self.registry.strategy_for(entry)
            if strategy is None or strategy.materialization == PASS_THROUGH:
                continue
            claimed.append((entry, strategy))

        if not claimed:
            # Legacy/manual quotes: a discount_amount with no usable breakdown
            # is treated as a single basket-level discount.
            from .basket import BasketDiscountStrategy

            entry = {
                "rule_name": "Discount",
                "amount": str(quote.discount_amount.amount),
                "base": str(quote.subtotal_price.amount),
            }
            claimed = [(entry, BasketDiscountStrategy())]

        for entry, strategy in claimed:
            eligible_kinds = strategy.eligible_kinds(entry)
            eligible_total = sum(
                (amount for kind, amount in per_kind.items() if kind in eligible_kinds),
                Decimal("0"),
            )
            if eligible_total <= 0:
                continue

            quote_base = strategy.quote_base(quote, entry)
            if quote_base <= 0:
                continue

            ratio = eligible_total / quote_base
            scaled = (Decimal(str(entry["amount"])) * ratio).quantize(TWO_PLACES)
            if scaled <= 0:
                continue

            if strategy.materialization == INVOICE_LEVEL:
                resolution.discount_amount += scaled
                scaled_entry = {**entry, "amount": str(scaled)}
                if entry.get("base") is not None:
                    scaled_entry["base"] = str(
                        (Decimal(str(entry["base"])) * ratio).quantize(TWO_PLACES)
                    )
                resolution.breakdown.append(scaled_entry)

            # The discount reduces the taxable base of the lines it was
            # computed on, spread proportionally across them — negative
            # lines (deductions, returns) included, so the bases still add
            # up to the eligible total minus the discount.
            for kind, amount in per_kind.items():
                if kind in eligible_kinds and amount != 0:
                    key = (quote.id, kind)
                    resolution._fractions[key] = (
                        resolution._fractions.get(key, Decimal("0"))
                        + scaled / eligible_total
                    )
