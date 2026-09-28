"""What to order: products promised on accepted quotes and not yet delivered.

Columns the previous system printed and this ERP cannot fill are left out:
there is no sales group, no ideal-stock target, and purchase orders that are
still draft or awaiting approval are not treated as incoming. Incoming is
only quantity on approved orders that has not been received yet — the same
figure stored on ``InventoryItem.incoming_quantity``.

Recommended quantity is whatever of the pending-to-serve amount is not
already covered by stock on hand or by that incoming quantity.
"""

from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO

from django.utils.translation import gettext as _

from inventory.choices import ItemType
from inventory.models import InventoryItemQuerySet


@dataclass(frozen=True)
class ReorderReportRow:
    item_id: int
    code: str
    name: str
    company_name: str
    stock: Decimal
    pending_to_serve: Decimal
    incoming: Decimal
    recommended: Decimal


def recommended_to_order(pending_to_serve, stock, incoming) -> Decimal:
    shortfall = Decimal(pending_to_serve) - Decimal(stock) - Decimal(incoming)
    if shortfall > 0:
        return shortfall
    return Decimal("0")


def reorder_report_rows(queryset) -> list[ReorderReportRow]:
    """One row per item that still has quantity pending to serve."""
    items = (
        queryset.exclude(item_type=ItemType.SERVICE)
        .select_related("company")
        .with_stock_on_hand()
        .annotate(
            pending_to_serve=InventoryItemQuerySet.pending_to_serve_expression(),
        )
        .filter(pending_to_serve__gt=0)
        .order_by("code", "pk")
    )
    rows = []
    for item in items:
        pending = item.pending_to_serve
        incoming = item.incoming_quantity
        stock = Decimal(item.stock_on_hand)
        rows.append(
            ReorderReportRow(
                item_id=item.pk,
                code=item.code,
                name=item.name or "",
                company_name=item.company.name if item.company_id else "",
                stock=stock,
                pending_to_serve=pending,
                incoming=incoming,
                recommended=recommended_to_order(pending, stock, incoming),
            )
        )
    return rows


def reorder_report_xlsx(rows, *, include_company=False) -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = str(_("What to order"))[:31]

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    quantity_format = "#,##0.###"

    headers = [str(_("Code")), str(_("Article"))]
    if include_company:
        headers.append(str(_("Company")))
    headers.extend([
        str(_("Stock")),
        str(_("Pending to serve")),
        str(_("Pending to receive (on the way)")),
        str(_("Recommended to order")),
    ])

    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment

    quantity_offset = 3 if include_company else 2
    for row_idx, row in enumerate(rows, start=2):
        values = [row.code, row.name]
        if include_company:
            values.append(row.company_name)
        values.extend([
            row.stock,
            row.pending_to_serve,
            row.incoming,
            row.recommended,
        ])
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col, value=value)
            if col > quantity_offset:
                cell.number_format = quantity_format

    widths = [16, 42]
    if include_company:
        widths.append(28)
    widths.extend([14, 22, 32, 22])
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    out = BytesIO()
    wb.save(out)
    return out.getvalue()
