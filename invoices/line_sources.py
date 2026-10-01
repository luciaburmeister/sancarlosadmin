"""
Where a SalesInvoiceItem came from, stored in its ``source_id`` as
``<prefix><pk>``.

This module is the only place that knows the format: build a source_id with
``source_id()``, read one with ``parse()``, and resolve the quote behind
lines with ``quote_ids_by_source()``. Adding a new kind of line means adding
its prefix here.
"""
from django.apps import apps

CONFIG = "config_"                  # configurator.Configuration on a quote
DIRECT = "direct_"                  # configurator.Configuration with no quote (shop / work order)
QUOTE_ITEM = "quote_item_"          # sales.QuoteItem
INV_ITEM = "inv_item_"              # sales.QuoteInventoryItem
WORK_ENTRY = "work_entry_"          # management.WorkEntry
DISCOUNT_QUOTE = "discount_quote_"  # legacy materialized financing line; pk is the quote's
ADVANCE_LINE = "advance_pf_"        # line of an advance invoice; pk is the project file's
ADVANCE_DEDUCTION = "advance_ded_"  # line deducting an advance; pk is the advance SalesInvoiceItem's

PREFIXES = (
    CONFIG, DIRECT, QUOTE_ITEM, INV_ITEM, WORK_ENTRY,
    DISCOUNT_QUOTE, ADVANCE_LINE, ADVANCE_DEDUCTION,
)

# Sources whose object holds a ``quote`` FK
QUOTE_SOURCE_MODELS = {
    CONFIG: "configurator.Configuration",
    DIRECT: "configurator.Configuration",
    QUOTE_ITEM: "sales.QuoteItem",
    INV_ITEM: "sales.QuoteInventoryItem",
}


def source_id(prefix, pk) -> str:
    """The source_id of the object ``pk`` behind a line of kind ``prefix``."""
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown invoice line source prefix: {prefix!r}")
    return f"{prefix}{pk}"


def parse(value):
    """``(prefix, pk)`` of a source_id, or ``(None, None)`` when it has none."""
    if value:
        for prefix in PREFIXES:
            if value.startswith(prefix):
                try:
                    return prefix, int(value[len(prefix):])
                except ValueError:
                    return None, None
    return None, None


def pk_of(value, prefix):
    """The pk in ``value`` when it is a source_id of kind ``prefix``, else None."""
    parsed_prefix, pk = parse(value)
    return pk if parsed_prefix == prefix else None


def quote_ids_by_source(values) -> dict:
    """
    ``{source_id: quote_id}`` for the lines of ``values`` that come from a
    quote, in one query per kind. Lines from no quote are left out.
    """
    pks_by_prefix = {}
    result = {}
    for value in values:
        prefix, pk = parse(value)
        if prefix == DISCOUNT_QUOTE:
            result[value] = pk
        elif prefix in QUOTE_SOURCE_MODELS:
            pks_by_prefix.setdefault(prefix, set()).add(pk)

    for prefix, pks in pks_by_prefix.items():
        model = apps.get_model(QUOTE_SOURCE_MODELS[prefix])
        rows = model.objects.filter(pk__in=pks, quote__isnull=False).values_list("pk", "quote_id")
        for pk, quote_id in rows:
            result[source_id(prefix, pk)] = quote_id
    return result


def quote_id_of(value):
    """The quote behind a single line's source_id, or None."""
    return quote_ids_by_source([value]).get(value)


def quote_source_ids(quote) -> list:
    """Every source_id a line billed from ``quote`` can carry."""
    return (
        [source_id(DISCOUNT_QUOTE, quote.pk)]
        + [source_id(CONFIG, pk) for pk in quote.configurations.values_list("pk", flat=True)]
        + [source_id(QUOTE_ITEM, pk) for pk in quote.items.values_list("pk", flat=True)]
        + [source_id(INV_ITEM, pk) for pk in quote.inventory_items.values_list("pk", flat=True)]
    )
