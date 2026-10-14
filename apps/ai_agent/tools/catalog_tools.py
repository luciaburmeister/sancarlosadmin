"""Generic ERP navigation primitives, built on apps.reports' dataset catalog.

These six tools are the entire "ERP tool layer" the agent needs — no new
tool gets written per report or question. Each operates on whichever
dataset the LLM names at call time, so each checks that dataset's own
permission inside ``execute()`` rather than declaring one static
``required_permission``.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.ai_agent.interfaces import BaseTool, ChatContext, ToolExecutionError
from apps.ai_agent.registry import register_tool
from apps.reports.constants import AggregationFunction, ChartType, FilterOperator
from apps.reports.datasets import all_datasets, datasets_access_for_user, get_dataset
from apps.reports.services.compiler import ReportCompiler, ReportPermissionDenied
from apps.reports.spec import ReportSpec, ReportSpecError

User = get_user_model()

_DATASET_ENUM = [dataset.key for dataset in all_datasets()]
_FILTER_OP_ENUM = [op.value for op in FilterOperator]
_AGG_FUNCTION_ENUM = [fn.value for fn in AggregationFunction]
_CHART_TYPE_ENUM = [chart.value for chart in ChartType]

_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "field": {"type": "string"},
        "op": {"type": "string", "enum": _FILTER_OP_ENUM},
        "value": {"type": ["string", "array"]},
    },
    "required": ["field", "op", "value"],
}


def _get_user(context: ChatContext):
    try:
        return User.objects.get(pk=context.user_id)
    except User.DoesNotExist as exc:
        raise ToolExecutionError("User not found.") from exc


def _raw_filters(params: dict) -> list[dict]:
    return [
        {"field": f["field"], "op": f["op"], "value": f["value"], "parameter": None}
        for f in (params.get("filters") or [])
    ]


def _json_cell(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def _measure_units(dataset, spec) -> dict[str, str]:
    units: dict[str, str] = {}
    for agg in spec.aggregations:
        if agg.function == AggregationFunction.COUNT:
            continue
        measure = dataset.measure_map().get(agg.measure)
        if measure and measure.unit:
            units[agg.alias] = measure.unit
    return units


def _chart_value_unit(spec, units: dict[str, str]) -> str:
    value_key = spec.chart_value_aggregation
    if not value_key and spec.chart == ChartType.NONE:
        counts = [
            agg.alias
            for agg in spec.aggregations
            if agg.function == AggregationFunction.COUNT
        ]
        value_key = counts[0] if counts else (
            spec.aggregations[0].alias if spec.aggregations else None
        )
    return units.get(value_key, "") if value_key else ""


def _column_labels(dataset, columns: list[str], units: dict[str, str] | None = None) -> list[str]:
    units = units or {}
    labels = []
    for key in columns:
        field = dataset.dimension_map().get(key)
        label = field.label if field is not None else key.replace("_", " ")
        unit = units.get(key)
        labels.append(f"{label} ({unit})" if unit else label)
    return labels


def _chart_payload(result, spec, units: dict[str, str]) -> dict | None:
    if not result.chart_labels:
        return None
    payload = {
        "type": result.chart_type,
        "labels": result.chart_labels,
        "values": result.chart_values,
    }
    if any(result.chart_urls):
        payload["urls"] = result.chart_urls
    value_unit = _chart_value_unit(spec, units)
    if value_unit:
        payload["value_unit"] = value_unit
    return payload


def _table_payload(dataset, spec, result) -> dict | None:
    if not result.rows:
        return None
    rows = []
    for index, row in enumerate(result.rows):
        urls = result.cell_urls[index] if index < len(result.cell_urls) else []
        rows.append(
            {
                "cells": [_json_cell(row.get(column)) for column in result.columns],
                "urls": list(urls),
            }
        )
    return {
        "columns": _column_labels(dataset, result.columns, _measure_units(dataset, spec)),
        "rows": rows,
    }


@register_tool
class ListDatasets(BaseTool):
    @property
    def name(self) -> str:
        return "list_datasets"

    @property
    def description(self) -> str:
        return (
            "List every ERP dataset (entity type) you can query, with a "
            "one-line label and whether the current user may access it. "
            "Call this first when unsure which dataset(s) a question "
            "needs, then call describe_dataset on the relevant ones "
            "before searching or aggregating."
        )

    def get_parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, context: ChatContext, params: dict) -> dict:
        user = _get_user(context)
        return {
            "datasets": [
                {
                    "key": access.key,
                    "label": access.label,
                    "available": access.available,
                }
                for access in datasets_access_for_user(user)
            ]
        }


@register_tool
class DescribeDataset(BaseTool):
    @property
    def name(self) -> str:
        return "describe_dataset"

    @property
    def description(self) -> str:
        return (
            "Get one dataset's full field catalog: dimensions (groupable/"
            "filterable fields), measures (summable/averageable fields), "
            "and its relations to other datasets (for traverse_relation). "
            "Call this before search_entities/aggregate/traverse_relation "
            "on a dataset you haven't described yet this conversation."
        )

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"dataset": {"type": "string", "enum": _DATASET_ENUM}},
            "required": ["dataset"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        try:
            dataset = get_dataset(params["dataset"])
        except KeyError as exc:
            raise ToolExecutionError(str(exc)) from exc

        catalog = dataset.catalog_for_prompt()
        catalog["relations"] = [
            {
                "key": relation.key,
                "target_dataset": relation.target_dataset,
                "kind": relation.kind,
                "label": relation.label,
            }
            for relation in dataset.relations
        ]
        return catalog


@register_tool
class GetEntity(BaseTool):
    @property
    def name(self) -> str:
        return "get_entity"

    @property
    def description(self) -> str:
        return (
            "Get one record by id from a dataset — e.g. one specific "
            "client, invoice, or project file. Use when the user names a "
            "specific record, or after search_entities/traverse_relation "
            "found the id you need full detail for."
        )

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": _DATASET_ENUM},
                "id": {"type": "integer"},
            },
            "required": ["dataset", "id"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        user = _get_user(context)
        try:
            row = ReportCompiler().get_by_id(params["dataset"], params["id"], user)
        except ReportPermissionDenied as exc:
            raise ToolExecutionError(f"No permission for dataset: {exc}") from exc
        except KeyError as exc:
            raise ToolExecutionError(str(exc)) from exc

        if row is None:
            return {"found": False}
        return {"found": True, "entity": row}


@register_tool
class TraverseRelation(BaseTool):
    @property
    def name(self) -> str:
        return "traverse_relation"

    @property
    def description(self) -> str:
        return (
            "Follow a relation from one entity to its related rows in "
            "another dataset — e.g. a client's project files, a project "
            "file's quotes or invoices, an invoice's client. Call "
            "describe_dataset first to see which relation keys exist "
            "on that dataset."
        )

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": _DATASET_ENUM},
                "id": {"type": "integer"},
                "relation": {
                    "type": "string",
                    "description": "A relation key from describe_dataset.",
                },
            },
            "required": ["dataset", "id", "relation"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        user = _get_user(context)
        try:
            rows = ReportCompiler().traverse_relation(
                params["dataset"], params["id"], params["relation"], user
            )
        except ReportPermissionDenied as exc:
            raise ToolExecutionError(f"No permission for dataset: {exc}") from exc
        except (KeyError, ReportSpecError) as exc:
            raise ToolExecutionError(str(exc)) from exc

        return {"rows": rows, "count": len(rows)}


@register_tool
class SearchEntities(BaseTool):
    @property
    def name(self) -> str:
        return "search_entities"

    @property
    def description(self) -> str:
        return (
            "List rows from a dataset matching optional filters — e.g. "
            "'invoices with payment_status unpaid', 'clients named "
            "García'. Returns each matching row's id plus the dimensions "
            "you asked for. For totals/sums/counts use aggregate instead."
        )

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": _DATASET_ENUM},
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dataset dimension keys to return per row.",
                },
                "filters": {"type": "array", "items": _FILTER_SCHEMA},
                "limit": {"type": "integer", "default": 25},
            },
            "required": ["dataset"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        user = _get_user(context)
        limit = min(int(params.get("limit") or 25), 100)

        spec_dict = {
            "dataset": params["dataset"],
            "dimensions": params.get("dimensions") or [],
            "aggregations": [
                {"measure": "*", "function": "count", "alias": "count"}
            ],
            "filters": _raw_filters(params),
            "parameters": [],
            "chart": "none",
            "chart_label_dimension": None,
            "chart_value_aggregation": None,
        }
        try:
            spec = ReportSpec.from_dict(spec_dict)
            result = ReportCompiler().compile(spec, user)
        except ReportPermissionDenied as exc:
            raise ToolExecutionError(f"No permission for dataset: {exc}") from exc
        except (KeyError, ReportSpecError) as exc:
            raise ToolExecutionError(str(exc)) from exc

        rows = [dict(row, id=pk) for row, pk in zip(result.rows, result.row_pks)]
        table = _table_payload(get_dataset(params["dataset"]), spec, result)
        if table is not None:
            table["rows"] = table["rows"][:limit]
        return {
            "rows": rows[:limit],
            "total_count": len(result.rows),
            "table": table,
        }


@register_tool
class Aggregate(BaseTool):
    @property
    def name(self) -> str:
        return "aggregate"

    @property
    def description(self) -> str:
        return (
            "Compute a sum/count/average, optionally grouped by one or "
            "more dimensions, with optional filters and a bar, line, or pie "
            "chart. Use for totals, counts per group, or trends — e.g. 'how "
            "much do we owe', 'invoices per client', 'billing by month'."
        )

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": _DATASET_ENUM},
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fields to group by. Empty means one overall total.",
                },
                "aggregations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "measure": {"type": "string"},
                            "function": {"type": "string", "enum": _AGG_FUNCTION_ENUM},
                            "alias": {"type": "string"},
                        },
                        "required": ["measure", "function", "alias"],
                    },
                },
                "filters": {"type": "array", "items": _FILTER_SCHEMA},
                "chart": {
                    "type": "string",
                    "enum": _CHART_TYPE_ENUM,
                    "default": "none",
                    "description": (
                        "bar compares categories. line is a trend over "
                        "time: use it for a month, week, day, or year "
                        "grouping unless the user asked for bars. pie shows "
                        "each share of a whole when there are few "
                        "categories. none skips the chart."
                    ),
                },
                "chart_label_dimension": {"type": ["string", "null"]},
                "chart_value_aggregation": {"type": ["string", "null"]},
            },
            "required": ["dataset", "aggregations"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        user = _get_user(context)
        spec_dict = {
            "dataset": params["dataset"],
            "dimensions": params.get("dimensions") or [],
            "aggregations": params["aggregations"],
            "filters": _raw_filters(params),
            "parameters": [],
            "chart": params.get("chart") or "none",
            "chart_label_dimension": params.get("chart_label_dimension"),
            "chart_value_aggregation": params.get("chart_value_aggregation"),
        }
        try:
            spec = ReportSpec.from_dict(spec_dict)
            result = ReportCompiler().compile(spec, user)
        except ReportPermissionDenied as exc:
            raise ToolExecutionError(f"No permission for dataset: {exc}") from exc
        except (KeyError, ReportSpecError) as exc:
            raise ToolExecutionError(str(exc)) from exc

        dataset = get_dataset(params["dataset"])
        units = _measure_units(dataset, spec)
        return {
            "rows": result.rows,
            "chart": _chart_payload(result, spec, units),
            "table": _table_payload(dataset, spec, result),
        }
