"""PDF artifact rendering — branded letterhead, narrative summary, then
one or more sections, each with its own KPI strip, optional chart, and
data table.

WeasyPrint is lazy-imported (same convention as core/services/pdf_service.py)
so the server can boot without its native libs (Pango/Cairo/GDK-Pixbuf)
configured; only generating a PDF needs them. Pagination, the running
letterhead, and the "page X / Y" footer are handled natively by WeasyPrint
(CSS @page + position: running()) so a long table simply flows onto as
many pages as it needs, instead of a hand-built fixed page count.
"""

from __future__ import annotations

from django.template.loader import render_to_string

from apps.ai_agent.artifacts.chart import scale_bars
from apps.ai_agent.artifacts.context import ArtifactContext


def build_pdf(context: ArtifactContext) -> bytes:
    import weasyprint

    html = render_to_string(
        "ai_agent/artifacts/report.html",
        {
            "title": context.title,
            "company_label": context.company_label,
            "generated_at": context.generated_at,
            "summary": context.summary,
            "sections": [
                {
                    "title": section.title,
                    "filters_summary": section.filters_summary,
                    "kpis": section.kpis,
                    "highlights": section.highlights,
                    "columns": section.columns,
                    "rows": [
                        [row.get(column) for column in section.columns]
                        for row in section.rows
                    ],
                    "chart_bars": (
                        scale_bars(section.chart_labels, section.chart_values)
                        if section.has_chart
                        else []
                    ),
                }
                for section in context.sections
            ],
        },
    )
    return weasyprint.HTML(string=html).write_pdf()
