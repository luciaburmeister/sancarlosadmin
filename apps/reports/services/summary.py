"""One-sentence description of a report spec, taken from the spec itself."""

from __future__ import annotations

from django.utils.translation import gettext as _
from apps.reports.constants import AggregationFunction
from apps.reports.datasets import get_dataset
from apps.reports.spec import ReportSpec


def describe_spec(spec: ReportSpec) -> str:
    dataset = get_dataset(spec.dataset)
    dimensions = dataset.dimension_map()
    measures = dataset.measure_map()
    grouping = ", ".join(
        dimensions[key].label for key in spec.dimensions if key in dimensions
    )
    metric = _metric_label(spec, measures, dimensions)
    start, end = _period(spec)
    dataset_label = dataset.label
    if grouping and start and end:
        return _(
            "%(metric)s of %(dataset)s from %(start)s to %(end)s, "
            "grouped by %(grouping)s."
        ) % {
            "metric": metric,
            "dataset": dataset_label,
            "start": start,
            "end": end,
            "grouping": grouping,
        }
    if grouping:
        return _("%(metric)s of %(dataset)s, grouped by %(grouping)s.") % {
            "metric": metric,
            "dataset": dataset_label,
            "grouping": grouping,
        }
    if start and end:
        return _("%(metric)s of %(dataset)s from %(start)s to %(end)s.") % {
            "metric": metric,
            "dataset": dataset_label,
            "start": start,
            "end": end,
        }
    return _("%(metric)s of %(dataset)s.") % {
        "metric": metric,
        "dataset": dataset_label,
    }


def _metric_label(spec: ReportSpec, measures, dimensions) -> str:
    aggregation = next(
        (
            agg
            for agg in spec.aggregations
            if agg.function == AggregationFunction.COUNT
        ),
        spec.aggregations[0] if spec.aggregations else None,
    )
    if aggregation is None:
        return _("Count")
    if aggregation.function == AggregationFunction.COUNT:
        return _("Count")
    measure = measures.get(aggregation.measure)
    label = measure.label if measure else aggregation.measure
    if aggregation.function == AggregationFunction.SUM:
        return _("Sum of %(measure)s") % {"measure": label}
    if aggregation.function == AggregationFunction.AVG:
        return _("Average of %(measure)s") % {"measure": label}
    if aggregation.function == AggregationFunction.COUNT_DISTINCT:
        dimension = dimensions.get(aggregation.measure)
        label = dimension.label if dimension else aggregation.measure
        return _("Distinct %(measure)s") % {"measure": label}
    return label


def _period(spec: ReportSpec) -> tuple[str, str]:
    start = ""
    end = ""
    for filt in spec.filters:
        if "date" not in filt.field:
            continue
        value = "" if filt.value is None else str(filt.value)
        if filt.op == "gte" and not start:
            start = value
        elif filt.op == "lte" and not end:
            end = value
    return start, end
