"""Reusable Excel workbook export.

Single source of the project's xlsx styling (teal header row, frozen
panes, autofilter, auto column width) — used by both the reports admin's
single-table export and the AI agent's multi-section artifact export, so
the look only needs to be defined once.
"""

from __future__ import annotations

import io
import re

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TEAL = "0D9488"
MUTED = "8A8A84"
_SHEET_NAME_MAX_LEN = 31
_INVALID_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")


class ExcelWorkbookBuilder:
    """Builds one .xlsx workbook, one worksheet per :meth:`add_sheet` call."""

    def __init__(self) -> None:
        self._workbook = openpyxl.Workbook()
        self._used_sheet_names: set[str] = set()
        self._sheet_count = 0

    def add_sheet(
        self,
        *,
        title: str,
        columns: list[str],
        rows: list[dict],
        company_label: str = "",
        filters_summary: list[str] | None = None,
        kpis: list | None = None,
        summary: str = "",
    ) -> None:
        """Add a worksheet with an optional header block above the table.

        ``kpis`` items need a ``label`` and a ``value``.
        """

        ws = self._workbook.active if self._sheet_count == 0 else self._workbook.create_sheet()
        ws.title = self._sheet_title(title, self._sheet_count)
        self._sheet_count += 1

        row = 1
        if title:
            ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=14)
            row += 1
        if company_label:
            ws.cell(row=row, column=1, value=company_label).font = Font(color=MUTED)
            row += 1
        if filters_summary:
            ws.cell(
                row=row, column=1, value=" · ".join(filters_summary)
            ).font = Font(italic=True, color=MUTED, size=9)
            row += 1
        if summary:
            ws.cell(row=row, column=1, value=summary)
            row += 1
        for kpi in kpis or []:
            ws.cell(
                row=row, column=1, value=f"{kpi.label}: {kpi.value}"
            ).font = Font(bold=True, color=TEAL)
            row += 1
        if row > 1:
            row += 1  # blank spacer before the table

        header_row = row
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor=TEAL)
        for col_idx, column in enumerate(columns, 1):
            cell = ws.cell(row=header_row, column=col_idx, value=column)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center")

        for offset, data_row in enumerate(rows, 1):
            for col_idx, column in enumerate(columns, 1):
                ws.cell(row=header_row + offset, column=col_idx, value=data_row.get(column))

        last_row = header_row + len(rows)
        last_col_letter = get_column_letter(len(columns) or 1)
        ws.freeze_panes = f"A{header_row + 1}"
        ws.auto_filter.ref = f"A{header_row}:{last_col_letter}{last_row}"

        for col_idx in range(1, len(columns) + 1):
            max_len = max(
                (
                    len(str(ws.cell(row=r, column=col_idx).value or ""))
                    for r in range(header_row, last_row + 1)
                ),
                default=10,
            )
            ws.column_dimensions[get_column_letter(col_idx)].width = min(max_len + 2, 40)

    def _sheet_title(self, raw: str, index: int) -> str:
        base = _INVALID_SHEET_CHARS.sub(" ", raw or f"Section {index + 1}").strip()
        base = base[:_SHEET_NAME_MAX_LEN] or f"Section {index + 1}"
        name = base
        suffix = 2
        while name in self._used_sheet_names:
            name = f"{base[: _SHEET_NAME_MAX_LEN - len(str(suffix)) - 1]}_{suffix}"
            suffix += 1
        self._used_sheet_names.add(name)
        return name

    def build(self) -> bytes:
        buffer = io.BytesIO()
        self._workbook.save(buffer)
        return buffer.getvalue()
