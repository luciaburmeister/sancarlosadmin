"""Excel artifact rendering — adapts an ArtifactContext (one or more
sections, each with a title/summary/KPI header above its data table) onto
``apps.reports``' shared :class:`ExcelWorkbookBuilder`, one worksheet per
section."""

from __future__ import annotations

from apps.ai_agent.artifacts.context import ArtifactContext
from apps.reports.services.excel_export import ExcelWorkbookBuilder


def build_excel(context: ArtifactContext) -> bytes:
    builder = ExcelWorkbookBuilder()
    for index, section in enumerate(context.sections):
        builder.add_sheet(
            title=section.title,
            columns=section.columns,
            rows=section.rows,
            company_label=context.company_label if index == 0 else "",
            filters_summary=section.filters_summary,
            kpis=section.kpis,
            summary=context.summary if index == 0 else "",
        )
    return builder.build()
