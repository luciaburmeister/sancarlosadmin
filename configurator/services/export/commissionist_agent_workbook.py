import re
from io import BytesIO
from typing import Any, Dict, NamedTuple

from django.db import models
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from core.constants import DATE_FORMAT, MONEY_FORMAT, PERCENTAGE_FORMAT


class AgentReportSheet(models.TextChoices):
    """The sheets an agent report workbook can have; the label is the sheet's title."""

    SETTLEMENT = "settlement", gettext_lazy("Settlement")
    INVOICES = "invoices", gettext_lazy("Invoices")
    INVOICE_LINES = "invoice_lines", gettext_lazy("Invoice lines")
    VAT_SUMMARY = "vat_summary", gettext_lazy("VAT summary")
    QUOTES = "quotes", gettext_lazy("Quotes")


class Column(NamedTuple):
    header: str
    key: str
    number_format: str | None = None
    width: int = 12


class AgentReportWorkbook:
    """
    An agent report as an Excel workbook: the tables the chosen agent_report
    template shows, one per sheet, from the same context its PDF renders
    (CommissionistAgentExportStrategy.get_context), with the PDF's columns.
    """

    CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    HEADER_ROW = 5

    def __init__(self, context: Dict[str, Any], template_html: str = ""):
        self.context = context
        self.agent = context["agent"]
        self.sheets = self.sheets_used_by(template_html)

    @staticmethod
    def sheets_used_by(html: str) -> list[AgentReportSheet]:
        """
        The tables a template shows, by the report data it reads, so a
        company's own templates export what they print. All of them when it
        reads none.
        """
        def reads(name):
            return re.search(rf"\b{name}\.", html) is not None

        sheets = []
        if reads("settlement"):
            sheets.append(AgentReportSheet.SETTLEMENT)
        if reads("invoices"):
            sheets.append(AgentReportSheet.INVOICES)
            if re.search(r"\.items\b", html):
                sheets.append(AgentReportSheet.INVOICE_LINES)
            if "vat_summary" in html:
                sheets.append(AgentReportSheet.VAT_SUMMARY)
        if reads("quotes"):
            sheets.append(AgentReportSheet.QUOTES)
        return sheets or list(AgentReportSheet)

    def render(self) -> bytes:
        import openpyxl

        tables = {
            AgentReportSheet.SETTLEMENT: self.settlement_table,
            AgentReportSheet.INVOICES: self.invoices_table,
            AgentReportSheet.INVOICE_LINES: self.invoice_lines_table,
            AgentReportSheet.VAT_SUMMARY: self.vat_summary_table,
            AgentReportSheet.QUOTES: self.quotes_table,
        }
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)
        for sheet in self.sheets:
            columns, rows, totals = tables[sheet]()
            self.write_sheet(workbook.create_sheet(str(sheet.label)[:31]), columns, rows, totals)
        out = BytesIO()
        workbook.save(out)
        return out.getvalue()

    def settlement_table(self):
        settlement = self.context["settlement"]
        columns = [
            Column(_("Date"), "date", DATE_FORMAT, 11),
            Column(_("Document"), "document", width=22),
            Column(_("Delivery notes"), "delivery_orders", width=22),
            Column(_("Client"), "client_code", width=9),
            Column(_("Name"), "client_name", width=36),
            Column(_("Base"), "base", MONEY_FORMAT, 13),
            Column(_("Commission %"), "percentage", PERCENTAGE_FORMAT, 12),
            Column(_("Commission"), "commission", MONEY_FORMAT, 13),
        ]
        return columns, settlement["rows"], settlement["totals"]

    def invoices_table(self):
        return self.document_table(self.context["invoices"])

    def quotes_table(self):
        return self.document_table(self.context["quotes"])

    def document_table(self, data):
        columns = [
            Column(_("Number"), "number", width=14),
            Column(_("Date"), "date", DATE_FORMAT, 11),
            Column(_("Client"), "client_code", width=9),
            Column(_("Name"), "client_name", width=36),
            Column(_("Agent"), "agent", width=8),
            Column(_("Payment Method"), "payment_method", width=16),
            Column(_("Base"), "base", MONEY_FORMAT, 13),
            Column(_("IRPF"), "retention", MONEY_FORMAT, 12),
            Column(_("VAT + surcharge"), "taxes", MONEY_FORMAT, 13),
            Column(_("Total"), "total", MONEY_FORMAT, 13),
        ]
        rows = [{**row, "agent": self.agent.pk} for row in data["rows"]]
        # As on the PDF, the totals row opens with the number of documents.
        return columns, rows, {**data["totals"], "number": len(rows)}

    def invoice_lines_table(self):
        columns = [
            Column(_("Invoice number"), "number", width=14),
            Column(_("Reference"), "reference", width=16),
            Column(_("Description"), "description", width=48),
            Column(_("Quantity"), "quantity", width=10),
            Column(_("Unit price"), "unit_price", MONEY_FORMAT, 13),
            Column(_("Discount %"), "discount_percentage", PERCENTAGE_FORMAT, 11),
            Column(_("Line total"), "line_total", MONEY_FORMAT, 13),
        ]
        rows = [
            {**item, "number": invoice["number"], "discount_percentage": item["discount_percentage"] or None}
            for invoice in self.context["invoices"]["rows"]
            for item in invoice["items"]
        ]
        return columns, rows, None

    def vat_summary_table(self):
        invoices = self.context["invoices"]
        columns = [
            Column(_("Tax"), "name", width=16),
            Column(_("Percentage"), "percentage", PERCENTAGE_FORMAT, 12),
            Column(_("Taxable base"), "base", MONEY_FORMAT, 15),
            Column(_("Tax due"), "amount", MONEY_FORMAT, 13),
            Column(_("Invoices"), "count", width=10),
        ]
        return columns, invoices["vat_summary"], {**invoices["vat_totals"], "name": _("Total")}

    def write_sheet(self, sheet, columns, rows, totals):
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        date_from, date_to = self.context["date_from"], self.context["date_to"]
        sheet["A1"] = self.agent.company.name
        sheet["A1"].font = Font(bold=True, size=12)
        sheet["A2"] = f"{_('Agent')}: {self.agent.pk} {self.agent.name}"
        sheet["A3"] = f"{_('Period')}: {date_from:%d/%m/%Y} – {date_to:%d/%m/%Y}"

        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        for col, column in enumerate(columns, start=1):
            cell = sheet.cell(row=self.HEADER_ROW, column=col, value=column.header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            sheet.column_dimensions[get_column_letter(col)].width = column.width

        row_idx = self.HEADER_ROW
        for row_idx, row in enumerate(rows, start=self.HEADER_ROW + 1):
            self.write_row(sheet, row_idx, columns, row)
        last_column = get_column_letter(len(columns))
        sheet.auto_filter.ref = f"A{self.HEADER_ROW}:{last_column}{row_idx}"
        sheet.freeze_panes = f"A{self.HEADER_ROW + 1}"

        if totals is not None:
            total_font = Font(bold=True)
            top_border = Border(top=Side(style="thin"))
            for cell in self.write_row(sheet, row_idx + 1, columns, totals):
                cell.font = total_font
                cell.border = top_border

    @staticmethod
    def write_row(sheet, row_idx, columns, values):
        cells = []
        for col, column in enumerate(columns, start=1):
            cell = sheet.cell(row=row_idx, column=col, value=values.get(column.key))
            if column.number_format:
                cell.number_format = column.number_format
            cells.append(cell)
        return cells
