"""
sales/analysis.py
─────────────────────────────────────────────────────────────────────────────
Quote-to-sale conversion analysis for the Polipus ERP.

Two item paths exist in the database
──────────────────────────────────────
  1. QuoteItem   — materials imported via the generic quote-item importer
                   (flat items, directly on a Quote).
                   Quantity field: QuoteItem.quantity

  2. ConfigurationItem — components that arrived via cortizo_import.py
                         (nested inside a Configuration which is linked to a
                         Quote).  The effective quantity is:
                           ConfigurationItem.quantity × ConfigurationItem.length
                         because Cortizo stores "n pieces × m metres" separately.

Both are normalised to a common identity key by:
  QuoteItem      → Material.reference_code
  ConfigurationItem → Component.code
When the same code appears in both paths the quantities are summed.

"Sold" definition
──────────────────
  An item is considered sold when its parent Quote has status == "accepted".

Deduplication (job-level)
──────────────────────────
  The same client may send multiple versions of a quote for the same job
  (same project_name). Only the canonical quote counts in the denominator:
    accepted > sent > draft/rejected; within same status, latest date wins.
  Quotes with no project_name are always kept individually.

Conversion % per item
──────────────────────
  sold_effective_qty / quoted_effective_qty × 100

Average conversion (fleet-wide)
────────────────────────────────
  Σ sold_effective_qty  /  Σ quoted_effective_qty  × 100
  (quantity-weighted)

Diagnosis thresholds (relative to the average)
────────────────────────────────────────────────
  Sin ventas   → 0 %
  Crítico      → 0 < conv  ≤  avg × 0.15
  Muy bajo     → avg × 0.15 < conv  ≤  avg × 0.45
  Bajo         → avg × 0.45 < conv  ≤  avg × 0.75
  Por mejorar  → avg × 0.75 < conv  <  avg
  Aceptable    → avg ≤ conv  < avg × 1.25
  Bueno        → avg × 1.25 ≤ conv  < avg × 1.75
  Excelente    → conv ≥ avg × 1.75

Margin classification (vs fleet avg margin)
────────────────────────────────────────────
  Por debajo  → avg_margin_pct < fleet_avg × 0.90
  Media       → fleet_avg × 0.90 ≤ avg_margin_pct ≤ fleet_avg × 1.10
  Por encima  → avg_margin_pct > fleet_avg × 1.10
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum, Count
from django.db.models.functions import Coalesce

from .models import QuoteItem, Quote


# ─────────────────────────────────────────────────────────────────────────────
# Diagnosis labels and CSS hints
# ─────────────────────────────────────────────────────────────────────────────

DIAGNOSIS_NO_SALES   = "Sin ventas"
DIAGNOSIS_CRITICAL   = "Crítico"
DIAGNOSIS_VERY_LOW   = "Muy bajo"
DIAGNOSIS_LOW        = "Bajo"
DIAGNOSIS_IMPROVE    = "Por mejorar"
DIAGNOSIS_ACCEPTABLE = "Aceptable"
DIAGNOSIS_GOOD       = "Bueno"
DIAGNOSIS_EXCELLENT  = "Excelente"

DIAGNOSIS_CSS = {
    DIAGNOSIS_NO_SALES:   "danger",
    DIAGNOSIS_CRITICAL:   "danger",
    DIAGNOSIS_VERY_LOW:   "warning",
    DIAGNOSIS_LOW:        "warning",
    DIAGNOSIS_IMPROVE:    "info",
    DIAGNOSIS_ACCEPTABLE: "success",
    DIAGNOSIS_GOOD:       "success",
    DIAGNOSIS_EXCELLENT:  "success",
}

ALL_DIAGNOSES = [
    DIAGNOSIS_NO_SALES,
    DIAGNOSIS_CRITICAL,
    DIAGNOSIS_VERY_LOW,
    DIAGNOSIS_LOW,
    DIAGNOSIS_IMPROVE,
    DIAGNOSIS_ACCEPTABLE,
    DIAGNOSIS_GOOD,
    DIAGNOSIS_EXCELLENT,
]

_ZERO    = Decimal("0")
_HUNDRED = Decimal("100")
_TWO_DP  = Decimal("0.01")

# Margin classification labels
MARGIN_ABOVE = "Por encima"
MARGIN_AT    = "Media"
MARGIN_BELOW = "Por debajo"
MARGIN_NA    = "-"


def get_company_wide_avg(company) -> Decimal:
    """
    Return the true company-wide average conversion %:
      total units sold / total units quoted × 100

    Uses the same canonical-quote deduplication as run_conversion_analysis
    (one quote per job, preferred by status then date) with NO other filters
    applied — no date range, no min_quoted, no article search.

    This is the stable benchmark shown in the KPI and used in 'vs. Media empresa'.
    """
    # Reuse the same deduplication helper with no date filters
    canonical_ids = _get_canonical_quote_ids(company, date_from=None, date_to=None)

    if not canonical_ids:
        return _ZERO

    qi_quoted, qi_sold = _collect_quote_items(company, None, None, canonical_ids)
    ci_quoted, ci_sold = _collect_config_items(company, None, None, None, canonical_ids)
    quoted_map, sold_map = _merge(qi_quoted, qi_sold, ci_quoted, ci_sold)

    total_quoted = sum(v["qty"] for v in quoted_map.values())
    total_sold   = sum(sold_map.values())

    if total_quoted == _ZERO:
        return _ZERO
    return _q(Decimal(str(total_sold)) / Decimal(str(total_quoted)) * _HUNDRED)


def _norm_code(code: str) -> str:
    """
    Normalise an article/component reference code so that codes which differ
    only by leading zeros are treated as the same item.
    e.g. "0400171" and "400171" both become "400171".
    Non-numeric codes (e.g. "CAP-001") are lowercased and stripped only.
    """
    stripped = (code or "").strip()
    if stripped.lstrip("0").isdigit() or stripped == "0":
        return stripped.lstrip("0") or "0"
    return stripped.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _diagnose(conv: Decimal, avg: Decimal, thresholds=None) -> str:
    """
    Classify an item's conversion % into a diagnosis label.

    If *thresholds* is provided (a ConversionThresholds instance) the
    boundaries are taken from it directly (absolute % values).
    Otherwise the original relative-to-average logic is used as a fallback.
    """
    if conv == _ZERO:
        return DIAGNOSIS_NO_SALES

    if thresholds is not None:
        c  = Decimal(str(thresholds.critical_pct))
        vl = Decimal(str(thresholds.very_low_pct))
        lo = Decimal(str(thresholds.low_pct))
        im = Decimal(str(thresholds.improve_pct))
        ac = Decimal(str(thresholds.acceptable_pct))
        go = Decimal(str(thresholds.good_pct))

        if conv <= c:
            return DIAGNOSIS_CRITICAL
        elif conv <= vl:
            return DIAGNOSIS_VERY_LOW
        elif conv <= lo:
            return DIAGNOSIS_LOW
        elif conv <= im:
            return DIAGNOSIS_IMPROVE
        elif conv <= ac:
            return DIAGNOSIS_ACCEPTABLE
        elif conv <= go:
            return DIAGNOSIS_GOOD
        else:
            return DIAGNOSIS_EXCELLENT

    # ── Fallback: original relative-to-average logic ──────────────────────────
    if avg == _ZERO:
        return DIAGNOSIS_EXCELLENT
    elif conv <= avg * Decimal("0.15"):
        return DIAGNOSIS_CRITICAL
    elif conv <= avg * Decimal("0.45"):
        return DIAGNOSIS_VERY_LOW
    elif conv <= avg * Decimal("0.75"):
        return DIAGNOSIS_LOW
    elif conv < avg:
        return DIAGNOSIS_IMPROVE
    elif conv < avg * Decimal("1.25"):
        return DIAGNOSIS_ACCEPTABLE
    elif conv < avg * Decimal("1.75"):
        return DIAGNOSIS_GOOD
    else:
        return DIAGNOSIS_EXCELLENT


def _q(value: Decimal) -> Decimal:
    return value.quantize(_TWO_DP, rounding=ROUND_HALF_UP)


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication — pick one canonical quote per job
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_RANK = {"accepted": 0, "sent": 1, "draft": 2, "rejected": 3}


def _get_canonical_quote_ids(company, date_from, date_to) -> frozenset:
    """
    For each (client_id, normalised project_name) group, keep only the
    canonical quote: accepted > sent > draft/rejected; latest date breaks ties.
    Quotes with no project_name are never merged (each is its own job).
    Returns a frozenset of quote PKs.
    """
    qs = Quote.objects.filter(company=company).values(
        "id", "project_name", "status", "date", "client_id"
    )
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    groups: dict = defaultdict(list)
    for q in qs:
        name = (q["project_name"] or "").strip()
        if name:
            key = (q["client_id"], name.lower())
        else:
            key = ("__anon__", q["id"])   # each nameless quote is unique
        groups[key].append(q)

    canonical_ids: set[int] = set()
    for quotes in groups.values():
        best = min(
            quotes,
            key=lambda q: (
                _STATUS_RANK.get(q["status"], 99),
                -(q["date"].toordinal() if q["date"] else 0),
            ),
        )
        canonical_ids.add(best["id"])

    return frozenset(canonical_ids)


# ─────────────────────────────────────────────────────────────────────────────
# Path 1 – QuoteItem  (flat materials, generic import)
# ─────────────────────────────────────────────────────────────────────────────

def _collect_quote_items(
    company, date_from, date_to, canonical_ids=None
) -> tuple[dict, dict]:
    """
    Returns:
        quoted_map  { reference_code: { name, qty, count, source } }
        sold_map    { reference_code: qty }

    When canonical_ids is provided the date filters are already embedded in
    that set, so we filter by quote PK instead of by date.
    """
    qs = QuoteItem.objects.filter(quote__company=company)
    if canonical_ids is not None:
        qs = qs.filter(quote_id__in=canonical_ids)
    else:
        if date_from:
            qs = qs.filter(quote__date__gte=date_from)
        if date_to:
            qs = qs.filter(quote__date__lte=date_to)

    quoted_agg = (
        qs
        .values("material__reference_code", "material__name")
        .annotate(
            total_qty=Coalesce(Sum("quantity"), _ZERO),
            line_count=Count("id"),
        )
    )
    sold_agg = (
        qs
        .filter(quote__status="accepted")
        .values("material__reference_code")
        .annotate(total_qty=Coalesce(Sum("quantity"), _ZERO))
    )

    quoted_map: dict[str, dict] = {}
    for row in quoted_agg:
        ref  = _norm_code(row["material__reference_code"] or "")
        name = row["material__name"] or ref
        if ref in quoted_map:
            quoted_map[ref]["qty"]   += row["total_qty"]
            quoted_map[ref]["count"] += row["line_count"]
        else:
            quoted_map[ref] = {
                "name":   name,
                "qty":    row["total_qty"],
                "count":  row["line_count"],
                "source": "quote_item",
            }

    sold_map: dict[str, Decimal] = {}
    for row in sold_agg:
        ref = _norm_code(row["material__reference_code"] or "")
        sold_map[ref] = sold_map.get(ref, _ZERO) + row["total_qty"]

    return quoted_map, sold_map


# ─────────────────────────────────────────────────────────────────────────────
# Path 2 – ConfigurationItem  (Cortizo / structured import)
# ─────────────────────────────────────────────────────────────────────────────

def _collect_config_items(
    company, date_from, date_to, status_filter=None, canonical_ids=None
) -> tuple[dict, dict]:
    """
    Returns:
        quoted_map  { component_code: { name, qty, count, source } }
        sold_map    { component_code: qty }
    """
    from configurator.models import ConfigurationItem

    qs = (
        ConfigurationItem.objects
        .filter(configuration__company=company)
        .filter(configuration__quote__isnull=False)
        .select_related("component", "configuration__quote")
    )
    if canonical_ids is not None:
        qs = qs.filter(configuration__quote_id__in=canonical_ids)
    else:
        if date_from:
            qs = qs.filter(configuration__quote__date__gte=date_from)
        if date_to:
            qs = qs.filter(configuration__quote__date__lte=date_to)
    if status_filter:
        qs = qs.filter(configuration__quote__status__in=status_filter)

    quoted_map: dict[str, dict] = defaultdict(
        lambda: {"qty": _ZERO, "count": 0, "name": "", "source": "config_item"}
    )
    sold_map: dict[str, Decimal] = defaultdict(lambda: _ZERO)

    for item in qs.iterator():
        if not item.component_id:
            continue

        code = _norm_code(item.component.code or "")
        name = item.component.name or code

        length  = item.length if (item.length and item.length > 0) else Decimal("1")
        eff_qty = item.quantity * length

        quoted_map[code]["qty"]   += eff_qty
        quoted_map[code]["count"] += 1
        quoted_map[code]["name"]   = name

        if item.configuration.quote.status == "accepted":
            sold_map[code] += eff_qty

    return dict(quoted_map), dict(sold_map)


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment — per-item financial & metadata from QuoteItems
# ─────────────────────────────────────────────────────────────────────────────

def _collect_quote_item_enrichment(canonical_ids) -> dict:
    """
    One pass over QuoteItems in the canonical set.
    For each material code, collects:
      • avg_sale_price   — average effective sale price on sold items
      • avg_margin_pct   — average margin % on sold items (None if no cost data)
      • profit_total     — Σ (revenue − cost) on sold items
      • agents           — sorted list of agent names from all canonical quotes
      • zones            — sorted list of sale zone names from all canonical quotes
      • category         — Material.category.name
      • mat_type         — Material.metadata["type"] if set
      • brand            — Material.metadata["brand"] if set
    """
    if not canonical_ids:
        return {}

    qs = (
        QuoteItem.objects
        .filter(quote_id__in=canonical_ids)
        .select_related(
            "material__category",
            "material__family",
            "quote__client__agent",
            "quote__client__sale_zone",
        )
    )

    data: dict[str, dict] = {}

    for item in qs.iterator():
        if not item.material_id:
            continue
        code = _norm_code(item.material.reference_code or "")

        if code not in data:
            data[code] = {
                "sale_prices":     [],
                "margin_samples":  [],
                "profit_total":    _ZERO,
                "agents":          set(),
                "zones":           set(),
                "quote_ids":       set(),
                "category":        item.material.category.name if item.material.category_id else "",
                "family":          item.material.family.name if item.material.family_id else "",
                "mat_type":        (item.material.metadata or {}).get("type", ""),
                "brand":           (item.material.metadata or {}).get("brand", ""),
                "quotes_detail":   [],   # list of {name, status, client, date, qty, price}
            }

        e = data[code]
        e["quote_ids"].add(item.quote_id)

        # Fill in metadata from first available record
        if not e["category"] and item.material.category_id:
            e["category"] = item.material.category.name
        if not e["family"] and item.material.family_id:
            e["family"] = item.material.family.name
        if not e["mat_type"]:
            e["mat_type"] = (item.material.metadata or {}).get("type", "")
        if not e["brand"]:
            e["brand"] = (item.material.metadata or {}).get("brand", "")

        # Per-quote detail row (all canonical quotes, not only sold)
        e["quotes_detail"].append({
            "pk":     item.quote_id,
            "name":   item.quote.project_name or f"#{item.quote_id}",
            "status": item.quote.status,
            "client": item.quote.client.name if item.quote.client_id else "-",
            "date":   item.quote.date,
            "qty":    item.quantity,
            "price":  item.unit_price.amount if item.unit_price else _ZERO,
        })

        # Agents and zones — from all canonical quotes (not only sold)
        client = item.quote.client if item.quote.client_id else None
        if client:
            if client.agent_id and client.agent:
                name = f"{client.agent.first_name} {client.agent.last_name}".strip()
                if name:
                    e["agents"].add(name)
            if client.sale_zone_id and client.sale_zone:
                e["zones"].add(client.sale_zone.name)

        # Financial metrics — only from accepted (sold) quotes
        if item.quote.status == "accepted":
            sale_price = item.unit_price.amount if item.unit_price else _ZERO
            cost_price = (
                item.material.cost_per_unit.amount
                if item.material.cost_per_unit else _ZERO
            )
            disc_factor = (Decimal("100") - item.discount) / Decimal("100")
            eff_price   = sale_price * disc_factor

            e["sale_prices"].append(eff_price)

            revenue = eff_price * item.quantity
            cost    = cost_price * item.quantity
            e["profit_total"] += revenue - cost

            if eff_price > _ZERO:
                margin = (eff_price - cost_price) / eff_price * _HUNDRED
                e["margin_samples"].append(margin)

    # Post-process: compute averages; convert sets to sorted lists
    result: dict[str, dict] = {}
    for code, e in data.items():
        avg_price = (
            _q(sum(e["sale_prices"]) / len(e["sale_prices"]))
            if e["sale_prices"] else _ZERO
        )
        avg_margin = (
            _q(sum(e["margin_samples"]) / len(e["margin_samples"]))
            if e["margin_samples"] else None
        )
        result[code] = {
            "avg_sale_price": avg_price,
            "avg_margin_pct": avg_margin,
            "profit_total":   _q(e["profit_total"]),
            "agents":         sorted(e["agents"]),
            "zones":          sorted(e["zones"]),
            "quote_count":    len(e["quote_ids"]),
            "category":       e["category"],
            "family":         e["family"],
            "mat_type":       e["mat_type"],
            "brand":          e["brand"],
            "quotes_detail":  sorted(e["quotes_detail"], key=lambda q: q["date"] or "", reverse=True),
        }

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Order conversion — how many orders contain each material
# ─────────────────────────────────────────────────────────────────────────────

def _collect_order_conversion(company) -> tuple[int, dict]:
    """
    Returns:
        total_orders  — total Order count for the company
        order_counts  — { norm_code: number of distinct orders containing it }
    """
    from .models import Order, OrderItem

    total_orders = Order.objects.filter(company=company).count()
    if not total_orders:
        return 0, {}

    counts: dict[str, set] = defaultdict(set)
    for item in (
        OrderItem.objects
        .filter(order__company=company)
        .select_related("material")
        .iterator()
    ):
        if item.material_id:
            code = _norm_code(item.material.reference_code or "")
            counts[code].add(item.order_id)

    return total_orders, {code: len(ids) for code, ids in counts.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Merge the two paths into unified dicts
# ─────────────────────────────────────────────────────────────────────────────

def _merge(
    qi_quoted: dict, qi_sold: dict,
    ci_quoted: dict, ci_sold: dict,
) -> tuple[dict, dict]:
    all_codes = set(qi_quoted) | set(ci_quoted)

    merged_quoted: dict[str, dict] = {}
    merged_sold:   dict[str, Decimal] = {}

    for code in all_codes:
        qi = qi_quoted.get(code)
        ci = ci_quoted.get(code)

        if qi and ci:
            merged_quoted[code] = {
                "name":   qi["name"] or ci["name"],
                "qty":    qi["qty"] + ci["qty"],
                "count":  qi["count"] + ci["count"],
                "source": "both",
            }
        elif qi:
            merged_quoted[code] = qi
        else:
            merged_quoted[code] = ci  # type: ignore[assignment]

        merged_sold[code] = (
            qi_sold.get(code, _ZERO) + ci_sold.get(code, _ZERO)
        )

    return merged_quoted, merged_sold


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_conversion_analysis(
    company,
    date_from=None,
    date_to=None,
    status_filter=None,      # list of Quote.status values for config items; None = all
    diagnosis_filter=None,   # list of diagnosis labels to keep; None = all
    search_query=None,       # free-text on name / reference code
    min_quoted=None,         # int — drop items with fewer total quoted qty than this
):
    """
    Return:
        {
          "summary": {
              total_quoted_qty, total_sold_qty, avg_conversion,
              total_materials, no_sales_count,
              avg_margin, total_orders,
              thresholds            ← ConversionThresholds instance or None
          },
          "rows": [
              { reference_code, material_name, quoted_qty, sold_qty,
                conversion_pct, diff_vs_avg, diagnosis, diagnosis_css, source,
                order_conv_pct, avg_sale_price, avg_margin_pct, margin_vs_avg,
                profit_total, agents, zones, category, mat_type, brand },
              …
          ]
        }

    Rows are sorted worst-first (lowest conversion_pct ascending).
    """

    # ── Load per-company thresholds (None → fallback to avg-relative logic) ───
    from .models import ConversionThresholds
    from django.db import ProgrammingError, OperationalError
    try:
        thresholds = ConversionThresholds.objects.get(company=company)
    except ConversionThresholds.DoesNotExist:
        thresholds = None
    except (ProgrammingError, OperationalError):
        # Table doesn't exist yet — migration hasn't been run.
        # Fall back gracefully to the avg-relative diagnosis logic.
        thresholds = None

    # ── Deduplication: one canonical quote per job ────────────────────────────
    canonical_ids = _get_canonical_quote_ids(company, date_from, date_to)

    # ── Collect quantities from both data paths ───────────────────────────────
    qi_quoted, qi_sold = _collect_quote_items(company, date_from, date_to, canonical_ids)
    ci_quoted, ci_sold = _collect_config_items(
        company, date_from, date_to, status_filter, canonical_ids
    )
    quoted_map, sold_map = _merge(qi_quoted, qi_sold, ci_quoted, ci_sold)

    # ── Enrichment (margin, price, agents, zones, category) ───────────────────
    enrichment = _collect_quote_item_enrichment(canonical_ids)

    # ── Build raw rows ────────────────────────────────────────────────────────
    total_quoted = _ZERO
    total_sold   = _ZERO
    raw_rows: list[dict] = []

    for code, info in quoted_map.items():
        q_qty = info["qty"]

        if min_quoted and q_qty < Decimal(min_quoted):
            continue

        s_qty    = sold_map.get(code, _ZERO)
        conv_pct = _q(s_qty / q_qty * _HUNDRED) if q_qty > _ZERO else _ZERO

        total_quoted += q_qty
        total_sold   += s_qty

        e = enrichment.get(code, {})

        raw_rows.append({
            "reference_code":  code,
            "material_name":   info["name"],
            "quoted_qty":      q_qty,
            "sold_qty":        s_qty,
            "conversion_pct":  conv_pct,
            "source":          info.get("source", ""),
            # enriched
            "quote_count":     e.get("quote_count", 0),
            "avg_sale_price":  e.get("avg_sale_price", _ZERO),
            "avg_margin_pct":  e.get("avg_margin_pct"),   # None = no cost data
            "profit_total":    e.get("profit_total", _ZERO),
            "agents":          e.get("agents", []),
            "zones":           e.get("zones", []),
            "category":        e.get("category", ""),
            "family":          e.get("family", ""),
            "mat_type":        e.get("mat_type", ""),
            "brand":           e.get("brand", ""),
            "quotes_detail":   e.get("quotes_detail", []),
        })

    # ── Global average conversion (used for diff_vs_avg in rows) ─────────────
    # We compute this from ALL raw_rows first so diagnosis labels are meaningful
    # (each item is judged relative to the overall company average, not to the
    # subset the user has filtered to).  The summary KPIs however always show
    # the filtered totals so the user can verify the numbers against the table.
    avg_conversion = (
        _q(total_sold / total_quoted * _HUNDRED)
        if total_quoted > _ZERO else _ZERO
    )

    # ── Global average margin ─────────────────────────────────────────────────
    margin_samples = [
        r["avg_margin_pct"] for r in raw_rows if r["avg_margin_pct"] is not None
    ]
    avg_margin = (
        _q(sum(margin_samples) / len(margin_samples))
        if margin_samples else _ZERO
    )

    # ── Assign diagnosis, diff vs avg, margin classification ─────────────────
    rows: list[dict] = []
    for r in raw_rows:
        diag = _diagnose(r["conversion_pct"], avg_conversion, thresholds)
        r["diagnosis"]     = diag
        r["diagnosis_css"] = DIAGNOSIS_CSS[diag]
        r["diff_vs_avg"]   = _q(r["conversion_pct"] - avg_conversion)

        m = r["avg_margin_pct"]
        if m is None:
            r["margin_vs_avg"] = MARGIN_NA
        elif avg_margin == _ZERO:
            r["margin_vs_avg"] = MARGIN_AT
        elif m > avg_margin * Decimal("1.10"):
            r["margin_vs_avg"] = MARGIN_ABOVE
        elif m < avg_margin * Decimal("0.90"):
            r["margin_vs_avg"] = MARGIN_BELOW
        else:
            r["margin_vs_avg"] = MARGIN_AT

        rows.append(r)

    # ── Post-filters ──────────────────────────────────────────────────────────
    if search_query:
        q_lower = search_query.lower()
        rows = [
            r for r in rows
            if q_lower in r["material_name"].lower()
            or q_lower in r["reference_code"].lower()
        ]

    if diagnosis_filter:
        rows = [r for r in rows if r["diagnosis"] in diagnosis_filter]

    # Sort: worst first
    rows.sort(key=lambda r: r["conversion_pct"])

    # ── Summary KPIs — computed from filtered rows ────────────────────────────
    no_sales_count   = sum(1 for r in rows if r["diagnosis"] == DIAGNOSIS_NO_SALES)
    with_sales_count = len(rows) - no_sales_count
    total_profit     = sum((r["profit_total"] for r in rows), _ZERO)

    filtered_quoted  = sum(r["quoted_qty"] for r in rows)
    filtered_sold    = sum(r["sold_qty"]   for r in rows)
    filtered_conv    = (
        _q(filtered_sold / filtered_quoted * _HUNDRED)
        if filtered_quoted > _ZERO else _ZERO
    )

    margin_samples_filtered = [
        r["avg_margin_pct"] for r in rows if r["avg_margin_pct"] is not None
    ]
    filtered_avg_margin = (
        _q(sum(margin_samples_filtered) / len(margin_samples_filtered))
        if margin_samples_filtered else _ZERO
    )

    # Obras únicas: count only canonical quotes that contain at least one
    # of the filtered articles (collected from their quotes_detail).
    filtered_quote_ids: set = set()
    for r in rows:
        for qd in r.get("quotes_detail", []):
            # quotes_detail entries don't store the PK directly, but we can
            # count unique (name, client) pairs as a proxy for unique jobs.
            filtered_quote_ids.add((qd.get("name", ""), qd.get("client", "")))
    filtered_canonical_count = len(filtered_quote_ids)

    summary = {
        "total_quoted_qty":  filtered_quoted,
        "total_sold_qty":    filtered_sold,
        "avg_conversion":    filtered_conv,
        "total_materials":   len(rows),
        "no_sales_count":    no_sales_count,
        "with_sales_count":  with_sales_count,
        "avg_margin":        filtered_avg_margin,
        "total_profit":      _q(total_profit),
        "canonical_count":   filtered_canonical_count,
        "thresholds":        thresholds,
    }

    return {"summary": summary, "rows": rows}