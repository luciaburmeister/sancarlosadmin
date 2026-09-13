"""Keeping :model:`inventory.InventoryItem`'s cached stock position in sync.

``incoming_quantity`` and ``reserved_quantity`` are denormalised columns: the
figures they hold are derived from purchase orders, quotes, delivery orders
and project files, which live in other apps and change through many different
code paths.

Two rules keep them trustworthy:

1. **Always recompute from source.** A refresh re-runs the aggregate defined in
   ``inventory.models`` and overwrites the column with the result. Nothing ever
   applies a delta to the stored value, so a missed or double-fired signal
   leaves a row momentarily stale rather than permanently wrong, and the next
   refresh of that item repairs it.

2. **One definition of the rule.** The aggregate lives on
   ``InventoryItemQuerySet`` (``incoming_expression()`` /
   ``reserved_expression()``) and is used both to write the cache and — by
   :func:`find_drift` and the tests — to audit it. There is no second
   implementation to fall out of step with.

Refreshes run inline, inside whatever transaction the triggering save is part
of, so a rolled-back purchase order takes its cache write down with it. The
price is that the transaction holds a row lock on every item it refreshed
until it commits; two transactions that touch overlapping items in a different
order can therefore deadlock, and PostgreSQL will abort one of them. Sorting
the ids inside one UPDATE does not prevent that — lock order within a single
statement follows the scan, not the IN list — so this is accepted rather than
solved; the writes are short and the conflict window narrow.

After deploying the columns, run ``manage.py refresh_stock_position`` once to
fill them in. It is deliberately not a data migration: the expressions run
against the live models, and a migration pinned to today's schema would break
a fresh ``migrate`` the day a referenced column is renamed.
"""

from django.db import models
from django.utils import timezone

from inventory.models import InventoryItem


def _refresh(queryset) -> int:
    """One UPDATE with the aggregates as correlated subqueries.

    No read-modify-write, so concurrent refreshes of the same item cannot lose
    each other's work.
    """
    return queryset.update(
        incoming_quantity=queryset.incoming_expression(),
        reserved_quantity=queryset.reserved_expression(),
        stock_position_refreshed_at=timezone.now(),
    )


def refresh_stock_position(item_ids) -> int:
    """Recompute the cached figures for ``item_ids``. Returns rows touched.

    Accepts any iterable, including a lazy ``values_list``; ``None`` entries
    (lines with no inventory item) are dropped.
    """
    ids = {i for i in item_ids if i is not None}
    if not ids:
        return 0
    return _refresh(InventoryItem.objects.filter(pk__in=ids))


def refresh_all() -> int:
    """Recompute every item's cached figures. Returns rows touched.

    Backs the ``refresh_stock_position`` management command — the post-deploy
    fill and the periodic repair.
    """
    return _refresh(InventoryItem.objects.all())


def find_drift(queryset=None):
    """Items whose cached figures disagree with a live recomputation.

    The audit that makes the cache checkable: it runs the very expressions the
    refresh writes from and returns every row where the stored value differs.
    A healthy system returns nothing.
    """
    queryset = InventoryItem.objects.all() if queryset is None else queryset
    return queryset.with_computed_stock_position().exclude(
        models.Q(incoming_quantity=models.F("computed_incoming"))
        & models.Q(reserved_quantity=models.F("computed_reserved"))
    )
