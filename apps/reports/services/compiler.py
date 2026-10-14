"""Compile a validated ReportSpec into a scoped ORM aggregation."""

from __future__ import annotations

from decimal import Decimal
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Avg, Count, DecimalField, F, Max, Min, Q, Sum
from django.db.models.functions import Coalesce
from django.urls import NoReverseMatch, reverse

from apps.reports.constants import (
    MAX_CHART_CATEGORIES,
    AggregationFunction,
    ChartType,
    FilterOperator,
    REPORT_ROW_CAP,
)
from apps.reports.datasets import BaseDataset, get_dataset
from apps.reports.spec import ReportSpec, ReportSpecError
from core.admin.company_access import get_user_companies


class ReportPermissionDenied(PermissionError):
    """Viewer lacks the dataset model permission."""


@dataclass
class ReportResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    chart_labels: list[str]
    chart_values: list[float]
    chart_type: str
    # Source record pk per row when its dimensions identify one record,
    # None for grouped rows. Lets the UI link a row to its admin page.
    row_pks: list[int | None] = field(default_factory=list)
    # Admin URL for the first column when that cell is one record.
    row_urls: list[str | None] = field(default_factory=list)
    # Admin URL per cell. Months, statuses, and totals stay None.
    cell_urls: list[list[str | None]] = field(default_factory=list)
    chart_urls: list[str | None] = field(default_factory=list)
    chart_is_top: bool = False


# A single identifier dimension is a listing, not a comparison.
_LISTING_DIMENSIONS = frozenset(
    {"invoice_number", "code", "internal_name", "reference", "number"}
)


def is_time_bucket(dimension: str | None) -> bool:
    """True when a dimension groups by month, week, day, or year."""
    if not dimension:
        return False
    return dimension.endswith(("_month", "_week", "_day", "_year"))


def chart_series(
    spec: ReportSpec,
    rows: list[dict[str, Any]],
    urls: list[str | None] | None = None,
) -> tuple[list[str], list[float], str, bool, list[str | None]]:
    """Bar/line points for the report. Large groupings keep the top categories."""

    chart_type = spec.chart
    label_key = spec.chart_label_dimension
    value_key = spec.chart_value_aggregation
    point_urls = list(urls or [])
    if chart_type == ChartType.NONE:
        if len(spec.dimensions) != 1 or spec.dimensions[0] in _LISTING_DIMENSIONS:
            return [], [], ChartType.NONE, False, []
        label_key = spec.dimensions[0]
        counts = [
            agg.alias
            for agg in spec.aggregations
            if agg.function == AggregationFunction.COUNT
        ]
        value_key = counts[0] if counts else (
            spec.aggregations[0].alias if spec.aggregations else None
        )
        chart_type = (
            ChartType.LINE if is_time_bucket(label_key) else ChartType.BAR
        )
    if not label_key or not value_key:
        return [], [], ChartType.NONE, False, []

    pairs: list[tuple[str, float, str | None]] = []
    for index, row in enumerate(rows):
        try:
            amount = float(row.get(value_key) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        url = point_urls[index] if index < len(point_urls) else None
        pairs.append((str(row.get(label_key) or ""), amount, url))
    is_top = len(pairs) > MAX_CHART_CATEGORIES
    if is_top:
        pairs.sort(key=lambda item: (-item[1], item[0]))
        pairs = pairs[:MAX_CHART_CATEGORIES]
    labels = [label for label, _amount, _url in pairs]
    values = [amount for _label, amount, _url in pairs]
    chart_urls = [url for _label, _amount, url in pairs]
    return labels, values, chart_type, is_top, chart_urls


def _safe_reverse(url_name: str | None, pk) -> str | None:
    if not url_name or pk is None:
        return None
    try:
        return reverse(url_name, args=[pk])
    except NoReverseMatch:
        return None


def _relation_url_name(dataset: BaseDataset, key: str) -> str | None:
    """Admin change view for a dimension that names one related record."""

    field = dataset.dimension_map().get(key)
    if field is None or "__" not in field.orm_path:
        return None
    accessor = field.orm_path.split("__", 1)[0]
    if accessor in {dataset.company_field, "company"}:
        return "admin:company_company_change"
    for relation in dataset.relations:
        if relation.kind != "to_one" or relation.orm_accessor != accessor:
            continue
        try:
            target = get_dataset(relation.target_dataset)
        except KeyError:
            return None
        return target.detail_url_name
    return None


def _entity_url(dataset: BaseDataset, key: str, raw: dict) -> str | None:
    url_name = _relation_url_name(dataset, key)
    if not url_name:
        return None
    min_id = raw.get(f"__min_{key}")
    max_id = raw.get(f"__max_{key}")
    if min_id is None or min_id != max_id:
        return None
    return _safe_reverse(url_name, min_id)


def _dimension_url(
    dataset: BaseDataset, key: str, raw: dict, row_pk
) -> str | None:
    """Link a category only when it is one company, client, invoice, or file."""

    entity = _entity_url(dataset, key, raw)
    if entity:
        return entity
    field = dataset.dimension_map().get(key)
    if field is None:
        return None
    if is_time_bucket(key) or field.allowed_values or field.boolean:
        return None
    if "__" in field.orm_path or row_pk is None:
        return None
    return _safe_reverse(dataset.detail_url_name, row_pk)


def _chart_label_key(spec: ReportSpec) -> str | None:
    if spec.chart != ChartType.NONE:
        return spec.chart_label_dimension
    if len(spec.dimensions) == 1 and spec.dimensions[0] not in _LISTING_DIMENSIONS:
        return spec.dimensions[0]
    return None


_AGG_MAP = {
    AggregationFunction.COUNT: Count,
    AggregationFunction.SUM: Sum,
    AggregationFunction.AVG: Avg,
}

_LOOKUP = {
    FilterOperator.EQ: "",
    FilterOperator.GT: "__gt",
    FilterOperator.GTE: "__gte",
    FilterOperator.LT: "__lt",
    FilterOperator.LTE: "__lte",
    FilterOperator.IN: "__in",
    FilterOperator.ICONTAINS: "__icontains",
}


class ReportCompiler:
    def get_by_id(
        self, dataset_key: str, pk: int, user
    ) -> dict[str, Any] | None:
        """Fetch one row's dimension/measure fields by primary key.

        Permission- and company-scoped exactly like ``compile()``. Returns
        ``None`` when the id doesn't exist or falls outside the user's
        company scope — never leaks across companies or without permission.
        """
        dataset = get_dataset(dataset_key)
        if not user.has_perm(dataset.permission):
            raise ReportPermissionDenied(dataset.permission)

        companies = get_user_companies(user)
        qs = dataset.base_queryset().filter(
            **{f"{dataset.company_field}__in": companies}, pk=pk
        )

        value_paths: dict[str, str] = {}
        for dim in dataset.dimensions:
            value_paths[dim.orm_path] = dim.key
        for measure in dataset.measures:
            value_paths.setdefault(measure.orm_path, measure.key)

        row = qs.values(*value_paths.keys()).first()
        if row is None:
            return None

        aliased = {alias: row[path] for path, alias in value_paths.items()}
        return self._serialize_row(aliased)

    def traverse_relation(
        self, dataset_key: str, pk: int, relation_key: str, user
    ) -> list[dict[str, Any]]:
        """Follow a declared ``DatasetRelation`` from one entity to the
        related dataset's rows — e.g. a project file's quotes, or a
        client's payments. Permission- and company-scoped on both sides.
        """
        dataset = get_dataset(dataset_key)
        if not user.has_perm(dataset.permission):
            raise ReportPermissionDenied(dataset.permission)

        relation = dataset.relation_map().get(relation_key)
        if relation is None:
            raise ReportSpecError(f"Unknown relation: {relation_key!r}")

        target = get_dataset(relation.target_dataset)
        if not user.has_perm(target.permission):
            raise ReportPermissionDenied(target.permission)

        companies = get_user_companies(user)

        if relation.kind == "to_many":
            target_qs = target.base_queryset().filter(
                **{f"{target.company_field}__in": companies},
                **{relation.orm_accessor: pk},
            )
        elif relation.kind == "to_one":
            source_qs = dataset.base_queryset().filter(
                **{f"{dataset.company_field}__in": companies}, pk=pk
            )
            fk_id = source_qs.values_list(
                f"{relation.orm_accessor}_id", flat=True
            ).first()
            if fk_id is None:
                return []
            target_qs = target.base_queryset().filter(
                **{f"{target.company_field}__in": companies}, pk=fk_id
            )
        else:
            raise ReportSpecError(f"Unknown relation kind: {relation.kind!r}")

        value_paths: dict[str, str] = {d.orm_path: d.key for d in target.dimensions}
        columns = ["pk", *value_paths.keys()]
        raw_rows = target_qs.values(*columns)[:REPORT_ROW_CAP]

        rows = []
        for raw in raw_rows:
            aliased = {alias: raw[path] for path, alias in value_paths.items()}
            aliased["id"] = raw["pk"]
            rows.append(self._serialize_row(aliased))
        return rows

    def compile(
        self,
        spec: ReportSpec,
        user,
        parameter_values: dict[str, Any] | None = None,
        companies=None,
    ) -> ReportResult:
        dataset = get_dataset(spec.dataset)
        if not user.has_perm(dataset.permission):
            raise ReportPermissionDenied(dataset.permission)

        self._validate_against_catalog(spec, dataset)

        if companies is None:
            companies = get_user_companies(user)
        qs = dataset.base_queryset().filter(
            **{f"{dataset.company_field}__in": companies}
        )

        related = set(dataset.select_related)
        for key in spec.dimensions:
            related.update(dataset.dimension_map()[key].select_related)
        if related:
            qs = qs.select_related(*related)

        qs = dataset.prepare_queryset(qs, spec)
        qs = self._apply_filters(qs, spec, dataset, parameter_values or {})

        try:
            return self._aggregate(qs, spec, dataset)
        except (ValueError, TypeError) as exc:
            raise ReportSpecError(
                f"Could not run the report with the current filters: {exc}"
            ) from exc

    def _aggregate(self, qs, spec: ReportSpec, dataset: BaseDataset) -> ReportResult:
        group_paths = []
        group_aliases = []
        for key in spec.dimensions:
            dim_field = dataset.dimension_map()[key]
            group_paths.append(dim_field.orm_path)
            group_aliases.append(key)

        annotations = {}
        for agg in spec.aggregations:
            if agg.function == AggregationFunction.COUNT:
                annotations[agg.alias] = Count("id")
            elif agg.function == AggregationFunction.COUNT_DISTINCT:
                dimension = dataset.dimension_map()[agg.measure]
                annotations[agg.alias] = Count(dimension.orm_path, distinct=True)
            else:
                fn = _AGG_MAP[agg.function]
                measure = dataset.measure_map()[agg.measure]
                annotations[agg.alias] = Coalesce(
                    fn(measure.orm_path),
                    Decimal("0"),
                    output_field=DecimalField(max_digits=18, decimal_places=4),
                )

        link_enabled = bool(group_paths) and bool(dataset.detail_url_name)
        if link_enabled:
            annotations["__min_id"] = Min("id")
            annotations["__max_id"] = Max("id")
        if group_paths:
            for key in spec.dimensions:
                if _relation_url_name(dataset, key) is None:
                    continue
                field = dataset.dimension_map()[key]
                id_orm = field.orm_path.rsplit("__", 1)[0] + "_id"
                annotations[f"__min_{key}"] = Min(id_orm)
                annotations[f"__max_{key}"] = Max(id_orm)

        if group_paths:
            qs = qs.values(*group_paths).annotate(**annotations).order_by(*group_paths)
        else:
            columns = [agg.alias for agg in spec.aggregations]
            totals = qs.aggregate(**annotations)
            row = {alias: totals.get(alias) for alias in columns}
            return ReportResult(
                columns=columns,
                rows=[self._serialize_row(row)],
                chart_labels=[],
                chart_values=[],
                chart_type=spec.chart,
                row_pks=[None],
                row_urls=[None],
                cell_urls=[[None] * len(columns)],
            )

        raw_rows = list(qs[:REPORT_ROW_CAP])
        columns = group_aliases + [a.alias for a in spec.aggregations]
        rows = []
        row_pks: list[int | None] = []
        row_urls: list[str | None] = []
        cell_urls: list[list[str | None]] = []
        point_urls: list[str | None] = []
        label_key = _chart_label_key(spec)
        for raw in raw_rows:
            row = {}
            for key, path in zip(group_aliases, group_paths):
                row[key] = raw.get(path)
            for agg in spec.aggregations:
                row[agg.alias] = raw.get(agg.alias)
            rows.append(self._serialize_row(row))

            min_id = raw.get("__min_id")
            max_id = raw.get("__max_id")
            row_pk = (
                min_id
                if link_enabled and min_id is not None and min_id == max_id
                else None
            )
            row_pks.append(row_pk)
            urls = [
                _dimension_url(dataset, key, raw, row_pk) for key in group_aliases
            ]
            urls.extend([None] * len(spec.aggregations))
            cell_urls.append(urls)
            row_urls.append(urls[0] if urls else None)
            point_urls.append(
                _dimension_url(dataset, label_key, raw, row_pk)
                if label_key
                else None
            )

        chart_labels, chart_values, chart_type, chart_is_top, chart_urls = (
            chart_series(spec, rows, point_urls)
        )
        return ReportResult(
            columns=columns,
            rows=rows,
            chart_labels=chart_labels,
            chart_values=chart_values,
            chart_type=chart_type,
            row_pks=row_pks,
            row_urls=row_urls,
            cell_urls=cell_urls,
            chart_urls=chart_urls,
            chart_is_top=chart_is_top,
        )

    def _validate_against_catalog(self, spec: ReportSpec, dataset: BaseDataset) -> None:
        dims = dataset.dimension_map()
        measures = dataset.measure_map()
        filters = dataset.filter_map()

        for key in spec.dimensions:
            if key not in dims:
                raise ReportSpecError(f"Unknown dimension: {key!r}")

        for agg in spec.aggregations:
            if agg.function == AggregationFunction.COUNT:
                continue
            if agg.function == AggregationFunction.COUNT_DISTINCT:
                if agg.measure not in dims:
                    raise ReportSpecError(f"Unknown dimension: {agg.measure!r}")
                continue
            if agg.measure not in measures:
                raise ReportSpecError(f"Unknown measure: {agg.measure!r}")

        for filt in spec.filters:
            if filt.field not in filters:
                raise ReportSpecError(f"Unknown filter field: {filt.field!r}")

    def _apply_filters(
        self,
        qs,
        spec: ReportSpec,
        dataset: BaseDataset,
        parameter_values: dict[str, Any],
    ):
        filter_map = dataset.filter_map()
        q = Q()
        for filt in spec.filters:
            value = filt.value
            if filt.parameter and filt.parameter in parameter_values:
                value = parameter_values[filt.parameter]
            if value is None or value == "":
                continue
            field = filter_map[filt.field]
            if field.low_stock:
                wanted = str(value).strip().casefold()
                if wanted not in {"true", "false"}:
                    raise ReportSpecError(
                        f"Invalid low-stock filter value {value!r}."
                    )
                low_q = Q(
                    has_components=False,
                    min_stock__isnull=False,
                    stock_on_hand__lte=F("min_stock"),
                )
                q &= low_q if wanted == "true" else ~low_q
                continue
            if field.boolean:
                wanted = str(value).strip().casefold()
                if wanted not in {"true", "false"}:
                    raise ReportSpecError(
                        f"Invalid boolean filter value {value!r} for field "
                        f"{filt.field!r}."
                    )
                q &= Q(**{field.orm_path: wanted == "true"})
                continue
            op = filt.op
            if field.text_match and op == FilterOperator.EQ:
                op = FilterOperator.ICONTAINS
            if op == FilterOperator.IN:
                if isinstance(value, str):
                    value = [
                        part.strip() for part in value.split(",") if part.strip()
                    ]
                if field.text_match:
                    # Exact __in would miss partial names like "San Carlos".
                    or_q = Q()
                    for part in value:
                        or_q |= Q(**{f"{field.orm_path}__icontains": part})
                    q &= or_q
                    continue
            elif field.numeric and isinstance(value, str):
                try:
                    value = Decimal(value.replace(",", "").strip())
                except Exception as exc:
                    raise ReportSpecError(
                        f"Invalid numeric filter value {value!r} for field "
                        f"{filt.field!r}."
                    ) from exc
            lookup = _LOOKUP[op]
            try:
                q &= Q(**{f"{field.orm_path}{lookup}": value})
            except (ValueError, TypeError) as exc:
                raise ReportSpecError(
                    f"Invalid filter value {value!r} for field {filt.field!r}."
                ) from exc
        try:
            return qs.filter(q) if q else qs
        except (ValueError, TypeError) as exc:
            raise ReportSpecError(
                "Invalid filter value for the current report. "
                "Use names for clients/employees, not numeric ids."
            ) from exc

    def _serialize_row(self, row: dict) -> dict[str, Any]:
        out = {}
        for key, value in row.items():
            if hasattr(value, "amount"):
                out[key] = round(float(value.amount), 2)
            elif hasattr(value, "isoformat"):
                out[key] = value.isoformat()
            elif isinstance(value, (float, Decimal)):
                out[key] = round(float(value), 2)
            else:
                out[key] = value
        return out
