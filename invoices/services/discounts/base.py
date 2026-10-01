"""Shared strategy interface for carrying quote discounts onto sales invoices.

A quote stores what was discounted and how in ``discount_breakdown`` (one
entry per applied ``DiscountRule``, tagged with ``level`` and ``kind`` by
``sales.services.discount_levels``). When that quote is invoiced — fully or
partially, alone or mixed with other quotes — each breakdown entry must be
carried onto the invoice in the way its configuration demands:

- line-item discounts are already baked into the source line's
  ``discount_percentage``/``line_total`` and only need to travel with the line;
- basket discounts (standard and financing) become part of the
  document-level ``discount_amount`` (+ a scaled ``discount_breakdown``
  entry each).

The same resolver carries quote discounts onto delivery orders.

Each of those behaviours is one ``InvoiceDiscountStrategy`` subclass (one per
file in this package). A strategy declares:

- ``matches(entry)``: which breakdown entries it claims;
- ``materialization``: how its scaled amount lands on the invoice
  (``INVOICE_LEVEL`` or ``PASS_THROUGH``);
- ``eligible_kinds(entry)``: which invoice line kinds form the entry's base,
  so partial invoices scale the discount by how much of *that base* they
  cover (a line-items-only discount must not scale with configuration lines,
  and one quote's discount must never bleed into another quote's lines);
- ``quote_base(quote, entry)``: the quote-side amount of that base.

To add a new behaviour: add a ``DiscountLevel``/``DiscountKind`` choice in
``sales.models``, add a file here with an ``InvoiceDiscountStrategy`` subclass,
and list it in ``registry.InvoiceDiscountStrategyRegistry.strategy_classes``
(order matters — first match claims the entry). Invoice-creation services
don't change: they all go through ``resolver.QuoteDiscountResolver``.
"""
from decimal import Decimal

from invoices import line_sources

# How a strategy's scaled amount lands on the invoice.
INVOICE_LEVEL = "invoice_level"   # add to invoice.discount_amount + discount_breakdown
PASS_THROUGH = "pass_through"     # already carried by the source line itself

# Invoice line kinds, derived from SalesInvoiceItem.source_id prefixes.
KIND_CONFIG = "config"        # config_<id> / direct_<id>
KIND_INV_ITEM = "inv_item"    # inv_item_<id>
KIND_QUOTE_ITEM = "quote_item"  # quote_item_<id>
# work_entry_<id> lines come from management.WorkEntry, not from a quote, so
# they never form part of a quote discount's base and are NOT in ALL_KINDS.
KIND_WORK_ENTRY = "work_entry"

ALL_KINDS = frozenset({KIND_CONFIG, KIND_INV_ITEM, KIND_QUOTE_ITEM})
LINE_ITEM_KINDS = frozenset({KIND_INV_ITEM, KIND_QUOTE_ITEM})

# source_id prefix of legacy financing lines on invoices created before
# financing moved into the header discount.
DISCOUNT_LINE_PREFIX = line_sources.DISCOUNT_QUOTE


def is_discount_line_source(source_id) -> bool:
    """Whether a source_id belongs to a legacy materialized discount line."""
    return bool(source_id) and source_id.startswith(DISCOUNT_LINE_PREFIX)


# source_id prefixes of advance invoicing. These lines are neither discounts
# nor goods: source_kind() maps them to None, so they never form part of a
# discount's base.
ADVANCE_LINE_PREFIX = line_sources.ADVANCE_LINE
ADVANCE_DEDUCTION_PREFIX = line_sources.ADVANCE_DEDUCTION


def is_advance_line_source(source_id) -> bool:
    """Whether a source_id belongs to a line of an advance invoice."""
    return bool(source_id) and source_id.startswith(ADVANCE_LINE_PREFIX)


def is_advance_deduction_source(source_id) -> bool:
    """Whether a source_id belongs to a line deducting an advance."""
    return bool(source_id) and source_id.startswith(ADVANCE_DEDUCTION_PREFIX)


_KIND_OF_PREFIX = {
    line_sources.CONFIG: KIND_CONFIG,
    line_sources.DIRECT: KIND_CONFIG,
    line_sources.INV_ITEM: KIND_INV_ITEM,
    line_sources.QUOTE_ITEM: KIND_QUOTE_ITEM,
    line_sources.WORK_ENTRY: KIND_WORK_ENTRY,
}


def source_kind(source_id):
    """Map a SalesInvoiceItem.source_id to its line kind (None if unknown)."""
    prefix, _pk = line_sources.parse(source_id)
    return _KIND_OF_PREFIX.get(prefix)


class InvoiceDiscountStrategy:
    """Base strategy for carrying one kind of quote discount onto an invoice."""

    materialization = INVOICE_LEVEL

    def matches(self, entry) -> bool:
        """Whether this strategy claims the given quote discount_breakdown entry."""
        raise NotImplementedError

    def eligible_kinds(self, entry):
        """Invoice line kinds that form this entry's base (default: all)."""
        return ALL_KINDS

    def quote_base(self, quote, entry) -> Decimal:
        """The quote-side base amount this entry's discount was computed on."""
        return Decimal(str(quote.subtotal_price.amount))


def entry_level(entry) -> str:
    """The entry's DiscountLevel, defaulting legacy entries to basket."""
    from sales.models import DiscountLevel

    return entry.get("level") or DiscountLevel.BASKET


def line_items_base(quote) -> Decimal:
    """Quote-side base for line-items-only discounts (excludes configurations)."""
    item_total, _config_subtotal = quote._calculate_subtotal()
    return item_total
