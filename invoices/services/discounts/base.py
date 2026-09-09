"""Shared strategy interface for carrying quote discounts onto sales invoices.

A quote stores what was discounted and how in ``discount_breakdown`` (one
entry per applied ``DiscountRule``, tagged with ``level`` and ``kind`` by
``sales.services.discount_levels``). When that quote is invoiced — fully or
partially, alone or mixed with other quotes — each breakdown entry must be
carried onto the invoice in the way its configuration demands:

- line-item discounts are already baked into the source line's
  ``discount_percentage``/``line_total`` and only need to travel with the line;
- standard basket discounts become part of the invoice-level
  ``discount_amount`` (+ a scaled ``discount_breakdown`` entry);
- financing discounts become their own negative invoice line, so the client
  sees them called out instead of folded into item prices.

Each of those behaviours is one ``InvoiceDiscountStrategy`` subclass (one per
file in this package). A strategy declares:

- ``matches(entry)``: which breakdown entries it claims;
- ``materialization``: how its scaled amount lands on the invoice
  (``INVOICE_LEVEL``, ``DISCOUNT_LINE`` or ``PASS_THROUGH``);
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

# How a strategy's scaled amount lands on the invoice.
INVOICE_LEVEL = "invoice_level"   # add to invoice.discount_amount + discount_breakdown
DISCOUNT_LINE = "discount_line"   # create a negative SalesInvoiceItem line
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


def source_kind(source_id):
    """Map a SalesInvoiceItem.source_id to its line kind (None if unknown)."""
    if not source_id:
        return None
    if source_id.startswith("config_") or source_id.startswith("direct_"):
        return KIND_CONFIG
    if source_id.startswith("inv_item_"):
        return KIND_INV_ITEM
    if source_id.startswith("quote_item_"):
        return KIND_QUOTE_ITEM
    if source_id.startswith("work_entry_"):
        return KIND_WORK_ENTRY
    return None


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

    def line_description(self, quote, entry) -> str:
        """Description for DISCOUNT_LINE materialization."""
        return entry.get("rule_name") or "Discount"


def entry_level(entry) -> str:
    """The entry's DiscountLevel, defaulting legacy entries to basket."""
    from sales.models import DiscountLevel

    return entry.get("level") or DiscountLevel.BASKET


def line_items_base(quote) -> Decimal:
    """Quote-side base for line-items-only discounts (excludes configurations)."""
    item_total, _config_subtotal = quote._calculate_subtotal()
    return item_total
