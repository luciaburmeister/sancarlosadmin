"""Shared contract for turning a prompt into a validated ReportSpec.

Provider-specific generators (OpenAI, Claude) only need to implement
``_fetch_payload`` — the catalog validation, filter pruning, and
SpecGeneration assembly live here once.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from django.utils import timezone

from apps.reports.constants import AggregationFunction
from apps.reports.datasets import get_dataset
from apps.reports.spec import (
    ReportSpec,
    ReportSpecError,
    prefer_count_for_grouped_chart,
)

logger = logging.getLogger(__name__)


@dataclass
class ReportGenerationContext:
    today: date = field(default_factory=lambda: timezone.localdate())
    company_names: list[str] = field(default_factory=list)
    client_names: list[str] = field(default_factory=list)


@dataclass
class SpecGeneration:
    spec: ReportSpec | None
    name: str
    summary: str = ""
    gap: str = ""


SPEC_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "dataset",
        "dimensions",
        "aggregations",
        "filters",
        "parameters",
        "chart",
        "chart_label_dimension",
        "chart_value_aggregation",
        "name",
        "can_express",
        "summary",
        "gap",
    ],
    "properties": {
        "dataset": {"type": "string"},
        "name": {"type": "string"},
        "dimensions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "aggregations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["measure", "function", "alias"],
                "properties": {
                    "measure": {"type": "string"},
                    "function": {
                        "type": "string",
                        "enum": [fn.value for fn in AggregationFunction],
                    },
                    "alias": {"type": "string"},
                },
            },
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "op", "value", "parameter"],
                "properties": {
                    "field": {"type": "string"},
                    "op": {
                        "type": "string",
                        "enum": ["eq", "gt", "gte", "lt", "lte", "in", "icontains"],
                    },
                    "value": {"type": ["string", "null"]},
                    "parameter": {"type": ["string", "null"]},
                },
            },
        },
        "parameters": {
            "type": "array",
            "items": {"type": "string"},
        },
        "chart": {"type": "string", "enum": ["bar", "line", "none"]},
        "chart_label_dimension": {"type": ["string", "null"]},
        "chart_value_aggregation": {"type": ["string", "null"]},
        "can_express": {"type": "boolean"},
        "summary": {"type": "string"},
        "gap": {"type": "string"},
    },
}


SPEC_SYSTEM_PROMPT = (
    "You design ERP report specifications. "
    "Use ONLY the fields listed in the catalog. "
    "Never invent field names. "
    "Set can_express false when the catalog has no field for a "
    "grouping, filter, or metric the user asked for. Then explain "
    "that in `gap`, in the user's language, in one sentence, and "
    "do not substitute a nearby field (payment_status is not a "
    "payment method). When can_express is true, `gap` must be an "
    "empty string and `summary` must be one sentence in the user's "
    "language describing the report you actually specified. "
    "When the user mentions a date or period in natural language "
    "(e.g. 'last month', 'este año', 'desde enero', 'Q1 2025', "
    "'últimos 90 días'), convert it to concrete ISO dates "
    "YYYY-MM-DD using today's date from the context. "
    "Put those dates as filter values AND expose date_from / date_to "
    "parameters so the UI can override them later. "
    "Every parameter name MUST appear on at least one filter's "
    "parameter field; never list orphan parameters. "
    "If there is no date filter, leave parameters empty. "
    "When the user wants a trend BY MONTH ('por mes', 'mensual', "
    "'monthly', 'evolución'), dimension by the dataset's *_month "
    "field (e.g. date_issued_month, date_month, payment_month, "
    "start_month) if the catalog has one — never the raw date "
    "field, which groups by exact day and produces one point per "
    "day instead of per month. "
    "For outstanding / pending / 'pendientes de cobro' invoices use "
    "payment_status with op 'in' and value 'unpaid,partial' "
    "(or eq 'unpaid'). "
    "When the user asks for invoices by payment method "
    "('método de pago', transferencia, efectivo, tarjeta), dimension "
    "by payment_method, not payment_status. payment_status is only "
    "paid, unpaid, or partial. "
    "When the user asks for amounts above/below a threshold "
    "('superan los 10k', 'over 10000', 'más de 5000'), ALWAYS add a "
    "numeric filter on total_amount (invoices), total_price (quotes), "
    "amount (payments), or stock_on_hand/min_stock (inventory) with "
    "op gt/gte/lt/lte and value like '10000'. Do not ignore "
    "thresholds. A filter's value is always a literal constant, "
    "never another field — 'X above Y' where both X and Y are "
    "fields (e.g. selling_price vs cost_price) cannot be expressed "
    "as a filter. For 'low stock' or 'stock bajo' inventory "
    "requests, use filter is_low_stock with op eq and value true "
    "(stock on hand at or below the item's min_stock; composite "
    "items are excluded). Do not invent a fixed threshold like "
    "stock_on_hand lt 5. If the user gives their own number "
    "('menos de 3 unidades'), use that numeric filter instead. "
    "For any other field-vs-field request, drop that filter rather "
    "than putting a field name in a numeric value. For 'valor de "
    "inventario' / 'inventory value' / 'cuánto vale el stock' "
    "requests, aggregate sum on the stock_value measure (already "
    "stock on hand × cost price — never compute this yourself as a "
    "filter or multiply other measures). "
    "To list individual invoices, dimension by invoice_number, "
    "client and date_issued (not only client). When the dimensions "
    "already make each row unique (e.g. invoice_number, a quote id), "
    "do NOT add a bare count aggregation — it is meaningless (always "
    "1) and clutters the table. Instead add a sum aggregation on the "
    "relevant amount measure (total_amount for invoices, "
    "total_price for quotes, amount for payments) so each row shows "
    "its own amount. Only add a count aggregation when the user "
    "wants a tally per group (accepted vs rejected, by method, "
    "by company, 'how many invoices per client'), not when listing "
    "individual rows. For a bar or line of that breakdown, set "
    "chart_value_aggregation to the count unless the user asked "
    "for money or time (amount, euros, invoiced, price, value, "
    "hours, duration); then chart that measure. "
    "count aggregations must use measure '*'. "
    "Filter values must be strings. "
    "active_company_names and client_names are a sample, not a "
    "closed list. Always put every company or client the user "
    "named into the filter, copying their words when the sample "
    "does not contain that name. Do not drop a name because it is "
    "missing from the sample; the server matches it afterwards. "
    "When the user names MORE THAN ONE company or client, use a "
    "single filter on that field with op 'in' and a comma-separated "
    "value — never pick only the first one and never add a second "
    "filter on the same field (two filters on the same field are "
    "ANDed together, which matches nothing). For a single "
    "company/client use op 'icontains'. "
    "Aggregation aliases MUST be snake_case ASCII identifiers "
    "(e.g. invoice_count), never spaces or accents. "
    "chart_value_aggregation must equal one of those aliases. "
    "aggregations must NEVER be empty — every report needs at least "
    "one, even a plain listing (use a sum on the relevant amount "
    "measure as already instructed, never leave the array empty). "
    "Chart selection: use chart 'none' whenever the dimensions "
    "already make each row unique (a listing, e.g. invoice_number "
    "or a per-item code+name) — a bar/line with one point per "
    "record is unreadable. Use 'bar' only for a grouped comparison "
    "across a small, bounded number of categories (roughly <= 15) "
    "such as status, client, warehouse, or item_type. Use 'line' "
    "only for a trend over a date/month dimension. When in doubt, "
    "or when the grouping could have many distinct values (e.g. "
    "grouping by client/item without a LIMIT-like filter), prefer "
    "'none' — the table is always shown regardless of chart. "
    "Use null for value only when a parameter has no sensible default. "
    "Return a short human name for the report. "
    "Stay minimal: include ONLY the dimensions, measures, and filters the "
    "request actually needs. Do not add an extra dimension, a second "
    "aggregation, or a related field just because it exists on the "
    "catalog or seems informative — every extra column makes the report "
    "harder to read, not better."
)


class BaseReportSpecGenerator(ABC):
    """Shared validation/assembly; subclasses only fetch the raw payload."""

    #: Exception type raised for failures specific to this provider.
    error_cls: type[Exception] = RuntimeError

    @abstractmethod
    def _fetch_payload(self, system: str, user: str, *, api_key: str = "") -> dict:
        """Call the provider and return the parsed JSON payload (a dict)."""

    def generate(
        self,
        prompt: str,
        domain: str,
        context: ReportGenerationContext | None = None,
        *,
        api_key: str = "",
    ) -> SpecGeneration:
        context = context or ReportGenerationContext()
        dataset = get_dataset(domain)
        catalog = dataset.catalog_for_prompt()

        context_json = json.dumps(
            {
                "today": context.today.isoformat(),
                "active_company_names": context.company_names,
                "client_names": context.client_names,
            },
            ensure_ascii=False,
        )
        user = (
            f"User request:\n{prompt}\n\n"
            f"Context (JSON):\n{context_json}\n\n"
            f"Dataset catalog (JSON):\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
            f"The dataset field MUST be exactly {domain!r}."
        )

        payload = self._fetch_payload(SPEC_SYSTEM_PROMPT, user, api_key=api_key)

        payload["dataset"] = domain
        name = (payload.pop("name", None) or "").strip()[:200]
        summary = (payload.pop("summary", None) or "").strip()
        gap = (payload.pop("gap", None) or "").strip()
        can_express = bool(payload.pop("can_express", True))
        if not can_express:
            return SpecGeneration(
                spec=None,
                name=name or "Untitled report",
                summary=summary,
                gap=gap or "The catalog cannot express this request.",
            )
        if not name:
            name = "Untitled report"
        try:
            spec = ReportSpec.from_dict(payload)
        except ReportSpecError as exc:
            raise self.error_cls(str(exc)) from exc

        dims = dataset.dimension_map()
        measures = dataset.measure_map()
        filters = dataset.filter_map()
        for key in spec.dimensions:
            if key not in dims:
                raise self.error_cls(f"Unknown dimension from model: {key!r}")
        for agg in spec.aggregations:
            if agg.function == AggregationFunction.COUNT:
                continue
            if agg.function == AggregationFunction.COUNT_DISTINCT:
                if agg.measure not in dims:
                    raise self.error_cls(
                        f"Unknown dimension from model: {agg.measure!r}"
                    )
                continue
            if agg.measure not in measures:
                raise self.error_cls(f"Unknown measure from model: {agg.measure!r}")

        kept_filters = []
        for filt in spec.filters:
            if filt.field in filters:
                kept_filters.append(filt)
            else:
                logger.warning(
                    "Dropping unknown filter field from model: %r", filt.field
                )

        if len(kept_filters) != len(spec.filters):
            remaining_params = {f.parameter for f in kept_filters if f.parameter}
            spec = ReportSpec(
                dataset=spec.dataset,
                dimensions=list(spec.dimensions),
                aggregations=list(spec.aggregations),
                filters=kept_filters,
                parameters=[p for p in spec.parameters if p in remaining_params],
                chart=spec.chart,
                chart_label_dimension=spec.chart_label_dimension,
                chart_value_aggregation=spec.chart_value_aggregation,
            )

        return SpecGeneration(
            spec=prefer_count_for_grouped_chart(spec, prompt),
            name=name,
            summary=summary,
            gap="",
        )
