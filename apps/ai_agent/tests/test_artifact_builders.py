import io
from datetime import datetime

import openpyxl
from django.test import SimpleTestCase

from apps.ai_agent.artifacts.chart import scale_bars
from apps.ai_agent.artifacts.context import ArtifactContext, ArtifactSection, KpiTile
from apps.ai_agent.artifacts.excel_builder import build_excel
from apps.ai_agent.artifacts.pdf_builder import build_pdf

COLUMNS = ["client", "total"]
ROWS = [{"client": "Acme SL", "total": 100.0}, {"client": "Beta SA", "total": 200.0}]

_DOC_FIELDS = {"title", "company_label", "generated_at", "summary"}


def make_context(**overrides) -> ArtifactContext:
    doc = dict(
        title="Report",
        company_label="Acme Co",
        generated_at=datetime(2026, 10, 5, 9, 0),
        summary="",
    )
    section = dict(title="Report", columns=COLUMNS, rows=ROWS)
    for key, value in overrides.items():
        if key in _DOC_FIELDS:
            doc[key] = value
        else:
            section[key] = value
    return ArtifactContext(sections=[ArtifactSection(**section)], **doc)


class ChartScalingTests(SimpleTestCase):
    def test_scales_relative_to_the_largest_value(self):
        bars = scale_bars(["a", "b"], [50, 100])
        self.assertEqual(bars[1].percent, 100)
        self.assertEqual(bars[0].percent, 50)

    def test_empty_input_returns_no_bars(self):
        self.assertEqual(scale_bars([], []), [])


class PdfBuilderTests(SimpleTestCase):
    def test_builds_nonempty_pdf(self):
        data = build_pdf(make_context())
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 500)

    def test_builds_pdf_with_summary_highlights_kpis_and_chart(self):
        context = make_context(
            summary="Billing grew steadily this month.",
            highlights=["Three clients account for most of the amount due."],
            kpis=[KpiTile(label="total", value=300.0)],
            chart_labels=["Jan", "Feb"],
            chart_values=[100.0, 200.0],
            chart_type="bar",
        )
        data = build_pdf(context)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 500)


class ExcelBuilderTests(SimpleTestCase):
    def test_builds_sheet_with_header_and_rows(self):
        data = build_excel(make_context())
        wb = openpyxl.load_workbook(io.BytesIO(data))
        ws = wb.active
        header_row = next(
            row
            for row in range(1, 10)
            if [c.value for c in ws[row]][: len(COLUMNS)] == COLUMNS
        )
        self.assertEqual(ws.cell(row=header_row + 1, column=1).value, "Acme SL")
        self.assertEqual(ws.cell(row=header_row + 2, column=2).value, 200.0)
        self.assertEqual(ws.freeze_panes, f"A{header_row + 1}")
        self.assertIsNotNone(ws.auto_filter.ref)

    def test_summary_and_kpis_appear_above_the_table(self):
        context = make_context(
            summary="Billing grew steadily this month.",
            kpis=[KpiTile(label="total", value=300.0)],
        )
        data = build_excel(context)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        ws = wb.active
        values = [cell.value for row in ws.iter_rows(max_col=1) for cell in row]
        self.assertIn("Billing grew steadily this month.", values)
        self.assertIn("total: 300.0", values)

    def test_multiple_sections_become_separate_sheets(self):
        context = ArtifactContext(
            title="Report",
            company_label="Acme Co",
            generated_at=datetime(2026, 10, 5, 9, 0),
            sections=[
                ArtifactSection(title="Ventas", columns=COLUMNS, rows=ROWS),
                ArtifactSection(
                    title="Producción",
                    columns=["worker", "hours"],
                    rows=[{"worker": "A", "hours": 5}],
                ),
            ],
        )
        data = build_excel(context)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(wb.sheetnames, ["Ventas", "Producción"])
        self.assertEqual(wb["Producción"].cell(row=1, column=1).value, "Producción")

    def test_duplicate_section_titles_get_unique_sheet_names(self):
        context = ArtifactContext(
            title="Report",
            company_label="Acme Co",
            generated_at=datetime(2026, 10, 5, 9, 0),
            sections=[
                ArtifactSection(title="Ventas", columns=COLUMNS, rows=ROWS),
                ArtifactSection(title="Ventas", columns=COLUMNS, rows=ROWS),
            ],
        )
        data = build_excel(context)
        wb = openpyxl.load_workbook(io.BytesIO(data))
        self.assertEqual(len(set(wb.sheetnames)), 2)
