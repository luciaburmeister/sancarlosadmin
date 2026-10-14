"""The agent's one document-generation tool: PDF or Excel.

Reuses the exact same validated ReportSpec -> ReportCompiler path as
aggregate()/search_entities() — an artifact is just "render this same
result as a file", with the same optional chart aggregate() supports,
plus a narrative summary/highlights Claude can author in the same call
so the document reads like a report rather than a raw export.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.ai_agent.artifacts.context import ArtifactContext, ArtifactSection, KpiTile
from apps.ai_agent.interfaces import BaseTool, ChatContext, ToolExecutionError
from apps.ai_agent.models import AIArtifact, AIArtifactKind
from apps.ai_agent.registry import register_tool
from apps.reports.constants import AggregationFunction, ChartType, FilterOperator
from apps.reports.datasets import all_datasets, get_dataset
from apps.reports.services.compiler import ReportCompiler, ReportPermissionDenied
from apps.reports.spec import ReportSpec, ReportSpecError
from core.admin.company_access import get_user_companies

User = get_user_model()

_DATASET_ENUM = [dataset.key for dataset in all_datasets()]
_CHART_TYPE_ENUM = [chart.value for chart in ChartType]
_MAX_KPI_TILES = 4
_MAX_COMPANY_NAMES = 3
_MAX_SECTIONS = 5
_MAX_HIGHLIGHTS = 5

_OPERATOR_SYMBOLS = {
    FilterOperator.EQ: "=",
    FilterOperator.GT: ">",
    FilterOperator.GTE: "≥",
    FilterOperator.LT: "<",
    FilterOperator.LTE: "≤",
    FilterOperator.IN: _("in"),
    FilterOperator.ICONTAINS: _("contains"),
}

_BUILDERS = {}


def _builders():
    if not _BUILDERS:
        from apps.ai_agent.artifacts.excel_builder import build_excel
        from apps.ai_agent.artifacts.pdf_builder import build_pdf

        _BUILDERS.update(
            {
                AIArtifactKind.PDF: (build_pdf, "pdf"),
                AIArtifactKind.EXCEL: (build_excel, "xlsx"),
            }
        )
    return _BUILDERS


def _field_label(dataset, key: str) -> str:
    for mapping in (dataset.filter_map(), dataset.dimension_map(), dataset.measure_map()):
        field = mapping.get(key)
        if field is not None:
            return field.label
    return key


def _describe_filters(dataset, filters) -> list[str]:
    described = []
    for flt in filters:
        label = _field_label(dataset, flt.field)
        op = _OPERATOR_SYMBOLS.get(flt.op, flt.op)
        value = ", ".join(flt.value) if isinstance(flt.value, list) else flt.value
        described.append(f"{label} {op} {value}")
    return described


def _company_label(user) -> str:
    companies = list(get_user_companies(user))
    if not companies:
        return ""
    names = [company.name for company in companies[:_MAX_COMPANY_NAMES]]
    label = " · ".join(names)
    if len(companies) > _MAX_COMPANY_NAMES:
        label += "…"
    return label


def _compute_kpis(aggregations, rows) -> list[KpiTile]:
    tiles: list[KpiTile] = []
    if len(rows) > 1:
        tiles.append(KpiTile(label=str(_("Rows")), value=len(rows)))
    for agg in aggregations:
        if len(tiles) >= _MAX_KPI_TILES:
            break
        if agg.function == AggregationFunction.AVG:
            continue
        total = sum((row.get(agg.alias) or 0) for row in rows)
        tiles.append(KpiTile(label=agg.alias, value=total))
    return tiles


@register_tool
class GenerateArtifact(BaseTool):
    @property
    def name(self) -> str:
        return "generate_artifact"

    @property
    def description(self) -> str:
        return (
            "Generate a downloadable PDF or Excel document — "
            "e.g. a report to send to a client, or a spreadsheet of a list "
            "you already showed in chat. Give it one or more `sections` "
            "(up to 5), one per dataset the user wants combined into this "
            "single document — e.g. sales + production in one PDF — each "
            "with the same dataset/dimensions/aggregations/filters you "
            "would give to aggregate(), optionally the same chart/"
            "chart_label_dimension/chart_value_aggregation too, and up to "
            "5 `highlights` specific to that section's data. Also pass a "
            "document-level `summary` (1-3 sentences) whenever the "
            "document should read as a report of findings rather than a "
            "raw export — write it and the highlights from what you "
            "already know about this data in the conversation, not "
            "generic filler."
        )

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["pdf", "xlsx"]},
                "title": {"type": "string"},
                "summary": {
                    "type": "string",
                    "description": (
                        "1-3 sentence narrative of what this document "
                        "shows as a whole, written for the person reading it."
                    ),
                },
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": _MAX_SECTIONS,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "dataset": {"type": "string", "enum": _DATASET_ENUM},
                            "dimensions": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "aggregations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "measure": {"type": "string"},
                                        "function": {
                                            "type": "string",
                                            "enum": [
                                                fn.value
                                                for fn in AggregationFunction
                                            ],
                                        },
                                        "alias": {"type": "string"},
                                    },
                                    "required": ["measure", "function", "alias"],
                                },
                            },
                            "filters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "field": {"type": "string"},
                                        "op": {"type": "string"},
                                        "value": {"type": ["string", "array"]},
                                    },
                                    "required": ["field", "op", "value"],
                                },
                            },
                            "chart": {"type": "string", "enum": _CHART_TYPE_ENUM},
                            "chart_label_dimension": {"type": ["string", "null"]},
                            "chart_value_aggregation": {"type": ["string", "null"]},
                            "highlights": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": _MAX_HIGHLIGHTS,
                                "description": (
                                    "Up to 5 short, specific findings worth "
                                    "calling out for this section."
                                ),
                            },
                        },
                        "required": ["title", "dataset", "aggregations"],
                    },
                },
            },
            "required": ["kind", "title", "sections"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        if context.conversation_id is None:
            raise ToolExecutionError(
                "No active conversation to attach the artifact to."
            )
        try:
            user = User.objects.get(pk=context.user_id)
        except User.DoesNotExist as exc:
            raise ToolExecutionError("User not found.") from exc

        kind = params["kind"]
        if kind not in _builders():
            raise ToolExecutionError(f"Unknown artifact kind: {kind!r}")

        section_params = params.get("sections") or []
        if not section_params:
            raise ToolExecutionError("At least one section is required.")
        if len(section_params) > _MAX_SECTIONS:
            raise ToolExecutionError(
                f"At most {_MAX_SECTIONS} sections are supported per document."
            )

        title = params["title"]
        companies = list(get_user_companies(user))

        sections: list[ArtifactSection] = []
        section_summaries: list[dict] = []
        for raw_section in section_params:
            spec_dict = {
                "dataset": raw_section["dataset"],
                "dimensions": raw_section.get("dimensions") or [],
                "aggregations": raw_section["aggregations"],
                "filters": [
                    {
                        "field": f["field"],
                        "op": f["op"],
                        "value": f["value"],
                        "parameter": None,
                    }
                    for f in (raw_section.get("filters") or [])
                ],
                "parameters": [],
                "chart": raw_section.get("chart") or "none",
                "chart_label_dimension": raw_section.get("chart_label_dimension"),
                "chart_value_aggregation": raw_section.get("chart_value_aggregation"),
            }
            try:
                spec = ReportSpec.from_dict(spec_dict)
                result = ReportCompiler().compile(spec, user, companies=companies)
            except ReportPermissionDenied as exc:
                raise ToolExecutionError(f"No permission for dataset: {exc}") from exc
            except (KeyError, ReportSpecError) as exc:
                raise ToolExecutionError(str(exc)) from exc

            dataset = get_dataset(raw_section["dataset"])
            sections.append(
                ArtifactSection(
                    title=raw_section["title"],
                    columns=result.columns,
                    rows=result.rows,
                    highlights=list(raw_section.get("highlights") or [])[
                        :_MAX_HIGHLIGHTS
                    ],
                    filters_summary=_describe_filters(dataset, spec.filters),
                    kpis=_compute_kpis(spec.aggregations, result.rows),
                    chart_labels=result.chart_labels,
                    chart_values=result.chart_values,
                    chart_type=result.chart_type,
                )
            )
            section_summaries.append(
                {
                    "title": raw_section["title"],
                    "dataset": raw_section["dataset"],
                    "row_count": len(result.rows),
                }
            )

        artifact_context = ArtifactContext(
            title=title,
            company_label=_company_label(user),
            generated_at=timezone.now(),
            summary=params.get("summary") or "",
            sections=sections,
        )

        build_fn, extension = _builders()[kind]
        file_bytes = build_fn(artifact_context)

        artifact = AIArtifact.objects.create(
            conversation_id=context.conversation_id,
            kind=kind,
            title=title,
        )
        artifact.file.save(
            f"{artifact.pk}.{extension}", ContentFile(file_bytes), save=True
        )

        return {
            "artifact_id": artifact.pk,
            "title": title,
            "kind": kind,
            "size": artifact.file.size,
            "row_count": sum(entry["row_count"] for entry in section_summaries),
            "sections": section_summaries,
            "download_url": reverse("ai_agent:download_artifact", args=[artifact.pk]),
        }
