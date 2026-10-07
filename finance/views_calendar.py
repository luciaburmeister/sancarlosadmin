# finance/views_calendar.py
from __future__ import annotations

import datetime
import io
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.auth.mixins import UserPassesTestMixin
from django.db.models import QuerySet
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

try:
    import openpyxl
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

from company.models import BankAccount, Company
from core.admin.company_access import get_user_companies
from core.dates import week_bounds

from .forms_calendar import PaymentCalendarFilterForm
from .services.payment_calendar import build_calendar_matrix

MAX_WINDOW_DAYS = 14

UNCONNECTED_CATEGORIES = {
    "Projected supplier payment plan",
    "Projected variable cost",
    "Work pending invoicing",
}


def _resolve_calendar_filters(request, companies: QuerySet[Company]) -> dict:
    """
    Parses company/bank_accounts/date_from/date_to from the request the same
    way for both the on-screen calendar and its Excel export, so the two
    never drift apart (e.g. if MAX_WINDOW_DAYS changes and only one caller
    gets updated).
    """
    raw_company_id = request.GET.get("company") or None
    company = companies.filter(pk=raw_company_id).first() if raw_company_id else None
    company = company or companies.first()

    company_bank_accounts = BankAccount.objects.filter(company=company) if company else BankAccount.objects.none()

    selected_account_ids = [account_id for account_id in request.GET.getlist("bank_accounts") if account_id]
    selected_bank_accounts = (
        company_bank_accounts.filter(pk__in=selected_account_ids)
        if selected_account_ids
        else company_bank_accounts
    )

    today = timezone.localdate()

    date_from_str = request.GET.get("date_from")
    date_to_str = request.GET.get("date_to")

    try:
        date_from = datetime.date.fromisoformat(date_from_str) if date_from_str else None
    except ValueError:
        date_from = None

    try:
        date_to = datetime.date.fromisoformat(date_to_str) if date_to_str else None
    except ValueError:
        date_to = None

    if not date_from or not date_to:
        date_from = today - datetime.timedelta(days=today.weekday())
        date_to = date_from + datetime.timedelta(days=6)

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    range_was_truncated = False
    max_date_to = date_from + datetime.timedelta(days=MAX_WINDOW_DAYS - 1)
    if date_to > max_date_to:
        date_to = max_date_to
        range_was_truncated = True

    return {
        "company": company,
        "company_bank_accounts": company_bank_accounts,
        "selected_account_ids": selected_account_ids,
        "selected_bank_accounts": selected_bank_accounts,
        "today": today,
        "date_from": date_from,
        "date_to": date_to,
        "range_was_truncated": range_was_truncated,
    }


def _build_kpis(matrix: dict) -> dict:
    saldo_cells = matrix["saldo_cells"]
    return {
        "opening_balance": saldo_cells[0]["value"],
        "closing_balance": saldo_cells[-1]["value"],
        "total_entradas": sum(cell["value"] for cell in matrix["entradas_cells"]),
        "total_salidas": sum(cell["value"] for cell in matrix["salidas_cells"]),
        "negative_days_count": sum(1 for cell in saldo_cells if cell["value"] < Decimal("0.00")),
    }


def _build_description_pivot(
    detail_cells: list[list], days: list[datetime.date]
) -> dict[str, dict[datetime.date, Decimal]]:
    """
    Collapses a category's per-day entry lists into a description ->
    {day: amount} pivot, so each distinct description (client, provider,
    loan installment, tax period, payroll period, etc.) becomes a single
    row spanning the full exported date range instead of a separate line
    per day. A day absent from the inner dict means no movement that day —
    rendered as a blank cell, not €0.00, to keep the two cases visually
    distinct. Works the same for every category, not just invoice-based
    ones, since CalendarEntry.description is populated for all of them.
    """
    pivot: dict[str, dict[datetime.date, Decimal]] = {}
    for day, day_entries in zip(days, detail_cells):
        for entry in day_entries:
            pivot.setdefault(entry.description, {})[day] = entry.amount
    return pivot


class PaymentCalendarView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Cashflow"
    permission_required = ()
    template_name = "admin/finance/payment_calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        companies = get_user_companies(request.user)

        filters = _resolve_calendar_filters(request, companies)
        company = filters["company"]
        company_bank_accounts = filters["company_bank_accounts"]
        selected_account_ids = filters["selected_account_ids"]
        selected_bank_accounts = filters["selected_bank_accounts"]
        today = filters["today"]
        date_from = filters["date_from"]
        date_to = filters["date_to"]
        range_was_truncated = filters["range_was_truncated"]

        form = PaymentCalendarFilterForm(
            data=request.GET if request.GET else None,
            companies=companies,
            bank_accounts=company_bank_accounts,
            initial={
                "company": company.pk if company else None,
                "bank_accounts": selected_account_ids,
                "date_from": date_from,
                "date_to": date_to,
            },
        )
        if request.GET:
            form.data = form.data.copy()
            form.data["date_from"] = date_from.isoformat()
            form.data["date_to"] = date_to.isoformat()

        matrix = build_calendar_matrix(company, selected_bank_accounts, date_from, date_to) if company else None
        kpis = _build_kpis(matrix) if matrix else None

        # The combined balance is only as fresh as its least recently synced
        # account, so surface the oldest last_balance_updated_at among the
        # accounts actually feeding the calendar.
        balance_updated_at = None
        if company:
            sync_timestamps = [
                account.last_balance_updated_at
                for account in selected_bank_accounts
                if account.last_balance_updated_at
            ]
            balance_updated_at = min(sync_timestamps) if sync_timestamps else None

        base_url = reverse("admin:finance_paymentcalendar_changelist")
        export_base_url = reverse("admin:finance_paymentcalendar_export")

        def _calendar_query_params(window_date_from: datetime.date, window_date_to: datetime.date) -> list[tuple[str, str]]:
            params = [("company", company.pk if company else "")]
            params += [("bank_accounts", account_id) for account_id in selected_account_ids]
            params += [
                ("date_from", window_date_from.isoformat()),
                ("date_to", window_date_to.isoformat()),
            ]
            return params

        def _build_url(window_date_from: datetime.date, window_date_to: datetime.date) -> str:
            return f"{base_url}?{urlencode(_calendar_query_params(window_date_from, window_date_to))}"

        window_length = (date_to - date_from).days + 1
        prev_date_from = date_from - datetime.timedelta(days=window_length)
        prev_date_to = date_to - datetime.timedelta(days=window_length)
        next_date_from = date_from + datetime.timedelta(days=window_length)
        next_date_to = date_to + datetime.timedelta(days=window_length)

        today_week_start, today_week_end = week_bounds(today)

        export_url = f"{export_base_url}?{urlencode(_calendar_query_params(date_from, date_to))}" if company else None

        context.update({
            "form": form,
            "selected_company": company,
            "date_from": date_from,
            "date_to": date_to,
            "today": today,
            "matrix": matrix,
            "kpis": kpis,
            "balance_updated_at": balance_updated_at,
            "range_was_truncated": range_was_truncated,
            "max_window_days": MAX_WINDOW_DAYS,
            "prev_week_url": _build_url(prev_date_from, prev_date_to),
            "next_week_url": _build_url(next_date_from, next_date_to),
            "today_url": _build_url(today_week_start, today_week_end),
            "export_url": export_url,
        })
        return context


class PaymentCalendarExportView(UserPassesTestMixin, View):
    """
    Export the payment calendar matrix to Excel, mirroring the on-screen
    structure and layering two levels of collapsible detail on top:

    - Level 0 (always visible): BANK BALANCE / OUTFLOWS / INFLOWS totals.
    - Level 1 (collapsed by default): one row per category, same order
      and totals as the dashboard.
    - Level 2 (collapsed by default, nested under its category): one row
      per distinct description within that category (client, provider,
      loan installment, tax period, payroll period...), pivoted across
      the full exported date range so a description spanning several
      days becomes a single row instead of one per day.

    A KPI block sits above the table (rows 1-2, mirroring the dashboard's
    four cards) written as live formulas referencing the table below, so
    they stay correct if someone edits a value in the sheet afterwards —
    they are not baked-in numbers. Category rows render identically
    whether or not their data source is wired in yet (unlike the on-screen
    version, which mutes unconnected categories) — this file is meant to
    be handed to someone outside the ERP, where that internal distinction
    isn't meaningful.

    Reuses the header/border/currency styling already used by
    TaxPeriodExportView (finance/views.py).
    """

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        if not HAS_OPENPYXL:
            return HttpResponse("openpyxl is required for Excel export.", status=500)

        companies = get_user_companies(request.user)
        filters = _resolve_calendar_filters(request, companies)
        company = filters["company"]

        if not company:
            return HttpResponse("No company selected.", status=400)

        matrix = build_calendar_matrix(
            company, filters["selected_bank_accounts"], filters["date_from"], filters["date_to"]
        )

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cashflow"

        ws.sheet_properties.outlinePr.summaryBelow = False
        ws.sheet_properties.outlinePr.showOutlineSymbols = True

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill("solid", fgColor="0D9488")
        center = Alignment(horizontal="center", vertical="center")
        money_fmt = '#,##0.00 "€"'
        date_fmt = "DD/MM/YYYY"
        thin = Side(style="thin", color="E2E8F0")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        bold_font = Font(bold=True)
        negative_fill = PatternFill("solid", fgColor="FFF1F2")  # --pf-rose-50
        negative_font = Font(bold=True, color="E11D48")  # --pf-rose-600
        today_fill = PatternFill("solid", fgColor="E7F8F6")  # ~10% --pf-teal-500, matches .pc-cell--today
        category_indent = Alignment(indent=1)
        detail_indent = Alignment(indent=2)
        zebra_fill = PatternFill("solid", fgColor="F8FAFC")  # --pf-bg, alternating band for detail rows

        # KPI styling
        kpi_label_font = Font(bold=True, size=9, color="64748B")  # --pf-text-sub
        kpi_label_fill = PatternFill("solid", fgColor="F8FAFC")
        kpi_value_teal_font = Font(bold=True, size=14, color="0D9488")
        kpi_value_rose_font = Font(bold=True, size=14, color="E11D48")
        kpi_box_border = Border(left=thin, right=thin, top=thin, bottom=thin)

        days = matrix["days"]
        today = timezone.localdate()
        last_day_col_letter = get_column_letter(len(days) + 1)
        today_col_idx = days.index(today) + 2 if today in days else None

        # ------------------------------------------------------------------
        # KPI block (rows 1-2) — written first as placeholders; the formulas
        # reference row numbers we only know once the table below is built,
        # so we fill in the actual formula strings after writing the table.
        # ------------------------------------------------------------------
        kpi_definitions = [
            (1, _("Current Balance"), kpi_value_teal_font),
            (3, _("Projected Balance"), kpi_value_teal_font),
            (5, _("Total Inflows"), kpi_value_teal_font),
            (7, _("Total Outflows"), kpi_value_rose_font),
        ]
        for col_start, label, value_font in kpi_definitions:
            label_range = f"{get_column_letter(col_start)}1:{get_column_letter(col_start + 1)}1"
            value_range = f"{get_column_letter(col_start)}2:{get_column_letter(col_start + 1)}2"
            ws.merge_cells(label_range)
            ws.merge_cells(value_range)
            label_cell = ws[label_range.split(":")[0]]
            label_cell.value = label.upper()
            label_cell.font = kpi_label_font
            label_cell.fill = kpi_label_fill
            label_cell.alignment = center
            label_cell.border = kpi_box_border
            value_cell = ws[value_range.split(":")[0]]
            value_cell.font = value_font
            value_cell.alignment = center
            value_cell.border = kpi_box_border
            value_cell.number_format = money_fmt
        ws.row_dimensions[1].height = 18
        ws.row_dimensions[2].height = 26
        # Row 3 stays blank as a visual separator between the KPI block and the table.

        # ------------------------------------------------------------------
        # Table — starts at row 4 (date header), shifted down 3 rows to make
        # room for the KPI block above it.
        # ------------------------------------------------------------------
        header_row = 4
        corner_cell = ws.cell(row=header_row, column=1, value="")
        corner_cell.fill = header_fill
        corner_cell.border = border
        for col_idx, day in enumerate(days, 2):
            cell = ws.cell(row=header_row, column=col_idx, value=day)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = border
            cell.number_format = date_fmt
        ws.row_dimensions[header_row].height = 22

        def _write_summary_row(
            row_idx: int, label: str, cells: list[dict], negate: bool = False, highlight_negative: bool = False
        ) -> None:
            label_cell = ws.cell(row=row_idx, column=1, value=label)
            label_cell.font = bold_font
            label_cell.border = border
            for col_idx, cell_data in enumerate(cells, 2):
                raw_value = cell_data["value"]
                cell = ws.cell(row=row_idx, column=col_idx, value=float(-raw_value if negate else raw_value))
                cell.font = bold_font
                cell.number_format = money_fmt
                cell.border = border
                if col_idx == today_col_idx:
                    cell.fill = today_fill
                if highlight_negative and raw_value < 0:
                    cell.fill = negative_fill
                    cell.font = negative_font

        def _write_category_row(row_idx: int, label: str, cells: list[dict], negate: bool) -> None:
            label_cell = ws.cell(row=row_idx, column=1, value=label)
            label_cell.border = border
            label_cell.alignment = category_indent
            for col_idx, cell_data in enumerate(cells, 2):
                raw_value = cell_data["value"]
                cell = ws.cell(row=row_idx, column=col_idx, value=float(-raw_value if negate else raw_value))
                cell.number_format = money_fmt
                cell.border = border
                if col_idx == today_col_idx:
                    cell.fill = today_fill
            ws.row_dimensions[row_idx].outlineLevel = 1
            ws.row_dimensions[row_idx].hidden = True

        def _write_detail_row(
            row_idx: int,
            description: str,
            values_by_day: dict[datetime.date, Decimal],
            negate: bool,
            shade: bool,
        ) -> None:
            label_cell = ws.cell(row=row_idx, column=1, value=description)
            label_cell.border = border
            label_cell.alignment = detail_indent
            if shade:
                label_cell.fill = zebra_fill
            for col_idx, day in enumerate(days, 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.border = border
                if shade:
                    cell.fill = zebra_fill
                if col_idx == today_col_idx:
                    cell.fill = today_fill
                if day in values_by_day:
                    raw_value = values_by_day[day]
                    cell.value = float(-raw_value if negate else raw_value)
                    cell.number_format = money_fmt
            ws.row_dimensions[row_idx].outlineLevel = 2
            ws.row_dimensions[row_idx].hidden = True

        def _write_category_block(start_row: int, rows: list[dict], negate: bool) -> int:
            row_idx = start_row
            for row in rows:
                label = str(row["label"])
                _write_category_row(row_idx, label, row["cells"], negate=negate)
                row_idx += 1
                pivot = _build_description_pivot(row["detail_cells"], days)
                for shade_idx, description in enumerate(sorted(pivot, key=str.lower)):
                    _write_detail_row(
                        row_idx, description, pivot[description], negate=negate, shade=(shade_idx % 2 == 1)
                    )
                    row_idx += 1
            return row_idx

        # Same top-to-bottom order as the dashboard: BANK BALANCE first,
        # then each total with its categories (and their description
        # detail) nested directly beneath it. Row numbers are captured as
        # we go so the KPI formulas above can reference them afterwards.
        row_idx = header_row + 1
        saldo_row = row_idx
        _write_summary_row(saldo_row, _("Bank Balance"), matrix["saldo_cells"], highlight_negative=True)
        row_idx += 1
        salidas_row = row_idx
        _write_summary_row(salidas_row, _("Outflows"), matrix["salidas_cells"], negate=True)
        row_idx += 1
        row_idx = _write_category_block(row_idx, matrix["salida_rows"], negate=True)
        entradas_row = row_idx
        _write_summary_row(entradas_row, _("Inflows"), matrix["entradas_cells"])
        row_idx += 1
        row_idx = _write_category_block(row_idx, matrix["entrada_rows"], negate=False)

        # ------------------------------------------------------------------
        # Now that saldo_row/salidas_row/entradas_row are known, fill in the
        # KPI formulas. These are live references, not baked-in numbers —
        # editing a value in the table recalculates the KPI above it.
        # ------------------------------------------------------------------
        ws["A2"] = f"=B{saldo_row}"
        ws["C2"] = f"={last_day_col_letter}{saldo_row}"
        ws["E2"] = f"=SUM(B{entradas_row}:{last_day_col_letter}{entradas_row})"
        # Stays negative on purpose — it references the OUTFLOWS row, which
        # is stored negative (see the "signo de las salidas" decision), so
        # this KPI ties exactly to what's on the sheet rather than a
        # separately-computed magnitude.
        ws["G2"] = f"=SUM(B{salidas_row}:{last_day_col_letter}{salidas_row})"

        for cell_ref in ("A2", "C2"):
            ws.conditional_formatting.add(
                cell_ref,
                CellIsRule(operator="lessThan", formula=["0"], font=Font(bold=True, size=14, color="E11D48")),
            )

        ws.column_dimensions[get_column_letter(1)].width = 32
        for col_idx in range(2, len(days) + 2):
            ws.column_dimensions[get_column_letter(col_idx)].width = 13

        ws.freeze_panes = f"B{header_row + 1}"

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        company_slug = slugify(str(company)) or "company"
        filename = f"cashflow_{company_slug}_{filters['date_from']}_{filters['date_to']}.xlsx"

        response = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response