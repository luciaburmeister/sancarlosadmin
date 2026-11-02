"""
Bulk stock-on-hand update from pasted spreadsheet rows.

Each row names an item code, the quantity physically counted, and optionally
a warehouse code (the item's default warehouse otherwise). The counted figure
is the new truth: the service records one ADJUST transaction per row for the
difference against the current balance in that warehouse, so the ledger stays
append-only, exactly like the single-item Adjust and "Reset stock to 0".

The whole paste is validated first and applied all-or-nothing: one bad row
and nothing is written.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction, Warehouse


@dataclass
class StockRow:
    line: int
    code: str
    raw_quantity: str
    warehouse_code: str = ""
    quantity: int | None = None
    item: InventoryItem | None = None
    warehouse: Warehouse | None = None
    current: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def delta(self) -> int:
        return (self.quantity or 0) - self.current

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass
class BulkStockPlan:
    rows: list[StockRow]
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return bool(self.rows) and not self.errors and all(r.is_valid for r in self.rows)

    @property
    def changes(self) -> list[StockRow]:
        return [r for r in self.rows if r.is_valid and r.delta]


def _delimiter(text: str) -> str:
    """Tab for a paste from Excel/Sheets, else ';' (Spanish CSV) or ','."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    if "\t" in first:
        return "\t"
    if ";" in first:
        return ";"
    return ","


_THOUSANDS_DOTS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def _parse_quantity(raw: str, delimiter: str) -> int | None:
    """Whole number from a cell, accepting Spanish formatting ("1.234", "12,0")."""
    value = raw.strip().replace(" ", "")
    if "," in value and delimiter != ",":
        value = value.replace(".", "").replace(",", ".")
    elif _THOUSANDS_DOTS.match(value):
        value = value.replace(".", "")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def parse_rows(text: str) -> list[StockRow]:
    delimiter = _delimiter(text)
    rows: list[StockRow] = []
    for index, cells in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), start=1):
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        code = cells[0]
        raw_quantity = cells[1] if len(cells) > 1 else ""
        quantity = _parse_quantity(raw_quantity, delimiter)
        if not rows and quantity is None and raw_quantity:
            continue  # header row
        row = StockRow(
            line=index,
            code=code,
            raw_quantity=raw_quantity,
            warehouse_code=cells[2] if len(cells) > 2 else "",
            quantity=quantity,
        )
        if not code:
            row.errors.append(str(_("Missing item code.")))
        if quantity is None:
            row.errors.append(str(_("Quantity must be a whole number.")))
        elif quantity < 0:
            row.errors.append(str(_("Quantity cannot be negative.")))
        rows.append(row)
    return rows


def build_plan(text: str, companies, *, lock: bool = False) -> BulkStockPlan:
    """Parse ``text`` and resolve every row against the items of ``companies``.

    With ``lock`` the matched items are locked for update, so the balances
    read here cannot move before the caller writes the adjustments.
    """
    rows = parse_rows(text)
    if not rows:
        return BulkStockPlan(rows=[], errors=[str(_("No rows to import."))])

    codes = {r.code for r in rows if r.code}
    items_qs = (
        InventoryItem.objects.filter(company__in=companies, code__in=codes)
        .select_related("default_warehouse")
    )
    if lock:
        items_qs = items_qs.select_for_update(of=("self",))
    items_by_code: dict[str, list[InventoryItem]] = {}
    for item in items_qs:
        items_by_code.setdefault(item.code, []).append(item)

    warehouse_codes = {r.warehouse_code for r in rows if r.warehouse_code}
    warehouses = {
        (w.company_id, w.code): w
        for w in Warehouse.objects.filter(company__in=companies, code__in=warehouse_codes)
    }

    for row in rows:
        if not row.code:
            continue
        matches = items_by_code.get(row.code, [])
        if not matches:
            row.errors.append(str(_("No item with this code.")))
            continue
        if len(matches) > 1:
            row.errors.append(str(_(
                "This code exists in more than one company; select a single company."
            )))
            continue
        item = row.item = matches[0]
        if item.item_type == ItemType.SERVICE:
            row.errors.append(str(_("Services hold no stock.")))
        elif item.is_composite:
            row.errors.append(str(_(
                "Composite item: it holds no stock of its own, update its components."
            )))
        if row.warehouse_code:
            row.warehouse = warehouses.get((item.company_id, row.warehouse_code))
            if row.warehouse is None:
                row.errors.append(str(_("No warehouse with this code in the item's company.")))
        else:
            row.warehouse = item.default_warehouse
            if row.warehouse is None:
                row.errors.append(str(_("The item has no default warehouse; add a warehouse column.")))

    seen: dict[tuple[int, int], int] = {}
    for row in rows:
        if row.item and row.warehouse:
            key = (row.item.pk, row.warehouse.pk)
            if key in seen:
                row.errors.append(
                    str(_("Duplicate of line %(line)d.")) % {"line": seen[key]}
                )
            else:
                seen[key] = row.line

    balances = {
        (b["item"], b["warehouse"]): b["total"] or 0
        for b in ItemTransaction.objects.filter(item__in=[k[0] for k in seen])
        .values("item", "warehouse")
        .annotate(total=Sum("quantity_change"))
    }
    for row in rows:
        if row.item and row.warehouse:
            row.current = balances.get((row.item.pk, row.warehouse.pk), 0)

    return BulkStockPlan(rows=rows)


def apply_bulk_stock(text: str, companies, *, user, note: str = "") -> BulkStockPlan:
    """Re-validate ``text`` under lock and write the adjustments atomically.

    Balances are re-read at apply time, so a movement recorded between the
    preview and the confirmation is accounted for: the counted figure wins.
    Returns the plan; when it is invalid nothing was written.
    """
    note = note.strip() or str(_("Bulk stock update"))
    with transaction.atomic():
        plan = build_plan(text, companies, lock=True)
        if not plan.is_valid:
            return plan
        ItemTransaction.objects.bulk_create([
            ItemTransaction(
                item=row.item,
                warehouse=row.warehouse,
                movement_type=MovementType.ADJUST,
                quantity_change=row.delta,
                user=user,
                note=note,
            )
            for row in plan.changes
        ])
        # bulk_create skips ItemTransaction.save(), which pushes stock to online stores.
        from apps.shopify.services.push_scheduler import ShopifyPushScheduler

        ShopifyPushScheduler.schedule_stock_push(row.item.pk for row in plan.changes)
    return plan
