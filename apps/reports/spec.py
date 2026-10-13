"""Validated report specification returned by the spec generator and stored on Report.spec."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from django.utils.translation import pgettext_lazy

from apps.reports.constants import (
    AggregationFunction,
    ChartType,
    FilterOperator,
    SUPPORTED_DOMAINS,
)


class ReportSpecError(ValueError):
    """Raised when a spec payload is invalid or outside the catalog."""


_MAGNITUDE_MARKERS = (
    ("amount", pgettext_lazy("report prompt", "amount")),
    ("euros", pgettext_lazy("report prompt", "euros")),
    ("invoiced", pgettext_lazy("report prompt", "invoiced")),
    ("billed", pgettext_lazy("report prompt", "billed")),
    ("billing", pgettext_lazy("report prompt", "billing")),
    ("price", pgettext_lazy("report prompt", "price")),
    ("value", pgettext_lazy("report prompt", "value")),
    ("worth", pgettext_lazy("report prompt", "worth")),
    ("hours", pgettext_lazy("report prompt", "hours")),
    ("duration", pgettext_lazy("report prompt", "duration")),
)


def _fold_prompt(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def _prompt_asks_for_magnitude(prompt: str) -> bool:
    raw = prompt or ""
    if "€" in raw:
        return True
    text = _fold_prompt(raw)
    for english, translated in _MAGNITUDE_MARKERS:
        for term in (english, str(translated)):
            folded = _fold_prompt(term)
            if folded and re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", text):
                return True
    return False


def prefer_count_for_grouped_chart(spec: ReportSpec, prompt: str) -> ReportSpec:
    """Point a bar/line chart at a row count when the prompt asks for a breakdown.

    A sum the model already added stays on the table. Money and duration
    prompts keep whatever measure the chart was given.
    """

    if spec.chart == ChartType.NONE:
        return spec
    if _prompt_asks_for_magnitude(prompt):
        return spec

    aggregations = list(spec.aggregations)
    count_alias = next(
        (
            agg.alias
            for agg in aggregations
            if agg.function == AggregationFunction.COUNT
        ),
        None,
    )
    if count_alias is None:
        count_alias = "row_count"
        used = {agg.alias for agg in aggregations}
        suffix = 2
        while count_alias in used:
            count_alias = f"row_count_{suffix}"
            suffix += 1
        aggregations.append(
            ReportAggregation(
                measure="*",
                function=AggregationFunction.COUNT,
                alias=count_alias,
            )
        )
    if (
        spec.chart_value_aggregation == count_alias
        and len(aggregations) == len(spec.aggregations)
    ):
        return spec
    return replace(
        spec,
        aggregations=aggregations,
        chart_value_aggregation=count_alias,
    )


_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_alias(raw: str, fallback: str = "value") -> str:
    """Turn a free-text alias into a safe ORM/SQL annotation name."""
    text = (raw or "").strip()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"v_{text}"
    text = text.encode("ascii", "ignore").decode("ascii") or fallback
    if not _ALIAS_RE.match(text):
        text = fallback
    return text[:60]


@dataclass(frozen=True)
class ReportAggregation:
    measure: str
    function: str
    alias: str


@dataclass(frozen=True)
class ReportFilter:
    field: str
    op: str
    value: Any
    parameter: str | None = None


@dataclass(frozen=True)
class ReportSpec:
    dataset: str
    dimensions: list[str] = field(default_factory=list)
    aggregations: list[ReportAggregation] = field(default_factory=list)
    filters: list[ReportFilter] = field(default_factory=list)
    parameters: list[str] = field(default_factory=list)
    chart: str = ChartType.NONE
    chart_label_dimension: str | None = None
    chart_value_aggregation: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ReportSpec:
        if not isinstance(data, dict):
            raise ReportSpecError("Spec must be an object.")

        allowed = {d.value for d in SUPPORTED_DOMAINS}
        dataset = data.get("dataset")
        if dataset not in allowed:
            raise ReportSpecError(f"Unknown dataset: {dataset!r}")

        chart = data.get("chart", ChartType.NONE)
        if chart not in ChartType.values:
            raise ReportSpecError(f"Unknown chart type: {chart!r}")

        aggregations = []
        used_aliases: set[str] = set()
        for index, raw in enumerate(data.get("aggregations") or []):
            if not isinstance(raw, dict):
                raise ReportSpecError("Each aggregation must be an object.")
            function = raw.get("function")
            if function not in AggregationFunction.values:
                raise ReportSpecError(f"Unknown aggregation: {function!r}")
            measure = raw.get("measure")
            alias = sanitize_alias(
                raw.get("alias") or "",
                fallback=f"metric_{index + 1}",
            )
            base = alias
            suffix = 2
            while alias in used_aliases:
                alias = f"{base}_{suffix}"
                suffix += 1
            used_aliases.add(alias)
            if not measure:
                raise ReportSpecError("Aggregation requires measure and alias.")
            if function == AggregationFunction.COUNT:
                # The compiler always counts rows regardless of which field
                # the model named, so normalize rather than reject — models
                # don't reliably follow the "measure must be '*'" instruction.
                measure = "*"
            elif function == AggregationFunction.COUNT_DISTINCT:
                if not measure or measure == "*":
                    raise ReportSpecError(
                        "count_distinct requires a dimension key as measure."
                    )
            elif measure == "*":
                raise ReportSpecError("sum/avg require a concrete measure.")
            aggregations.append(
                ReportAggregation(measure=measure, function=function, alias=alias)
            )

        if not aggregations:
            # The compiler always needs at least one aggregation column, but
            # models don't reliably add one for a plain listing despite the
            # instruction to — default to a row count rather than reject.
            aggregations.append(
                ReportAggregation(measure="*", function=AggregationFunction.COUNT, alias="count")
            )

        filters = []
        for raw in data.get("filters") or []:
            if not isinstance(raw, dict):
                raise ReportSpecError("Each filter must be an object.")
            op = raw.get("op")
            if op not in FilterOperator.values:
                raise ReportSpecError(f"Unknown filter operator: {op!r}")
            field_key = raw.get("field")
            if not field_key:
                raise ReportSpecError("Filter requires field.")
            filters.append(
                ReportFilter(
                    field=field_key,
                    op=op,
                    value=raw.get("value"),
                    parameter=raw.get("parameter"),
                )
            )

        dimensions = list(data.get("dimensions") or [])
        parameters = list(data.get("parameters") or [])
        chart_label_dimension = data.get("chart_label_dimension")
        chart_value_aggregation = data.get("chart_value_aggregation")
        if chart_value_aggregation:
            sanitized_chart = sanitize_alias(chart_value_aggregation)
            aliases = {a.alias for a in aggregations}
            if chart_value_aggregation in aliases:
                pass
            elif sanitized_chart in aliases:
                chart_value_aggregation = sanitized_chart
            else:
                chart_value_aggregation = aggregations[0].alias

        if chart != ChartType.NONE:
            if not chart_label_dimension or chart_label_dimension not in dimensions:
                raise ReportSpecError(
                    "chart_label_dimension must be one of the dimensions."
                )
            aliases = {a.alias for a in aggregations}
            if not chart_value_aggregation or chart_value_aggregation not in aliases:
                raise ReportSpecError(
                    "chart_value_aggregation must be an aggregation alias."
                )

        param_names = {f.parameter for f in filters if f.parameter}
        parameters = [name for name in parameters if name in param_names]

        dataset_value = getattr(dataset, "value", dataset)

        return cls(
            dataset=dataset_value,
            dimensions=dimensions,
            aggregations=aggregations,
            filters=filters,
            parameters=parameters,
            chart=chart,
            chart_label_dimension=chart_label_dimension,
            chart_value_aggregation=chart_value_aggregation,
        )
