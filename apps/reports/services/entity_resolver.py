"""Resolve free-text names against the user's active companies and their clients."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher

from django.utils.translation import gettext as _

from apps.reports.constants import FilterOperator
from apps.reports.datasets import get_dataset
from apps.reports.spec import ReportFilter, ReportSpec
from core.admin.company_access import get_user_companies

MAX_NAMES_FOR_PROMPT = 120
NAME_MATCH_THRESHOLD = 0.72


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().strip()
    return re.sub(r"\s+", " ", text)


def best_name_match(raw: str, candidates: list[str]) -> str | None:
    needle = _normalize(raw)
    if not needle or not candidates:
        return None

    exact = [c for c in candidates if _normalize(c) == needle]
    if exact:
        return exact[0]

    contained = [
        c
        for c in candidates
        if needle in _normalize(c) or _normalize(c) in needle
    ]
    if len(contained) == 1:
        return contained[0]
    if len(contained) > 1:
        return min(contained, key=lambda c: (len(c), c.casefold()))

    scored = []
    for candidate in candidates:
        ratio = SequenceMatcher(None, needle, _normalize(candidate)).ratio()
        if ratio >= NAME_MATCH_THRESHOLD:
            scored.append((ratio, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1].casefold()))
    return scored[0][1]


def active_company_names(user, limit: int | None = MAX_NAMES_FOR_PROMPT) -> list[str]:
    """Company names, capped by default for the OpenAI prompt's context.

    Pass limit=None to resolve a name against the full list — matching
    against a small illustrative sample would silently miss any real
    company/client past the cutoff.
    """
    qs = get_user_companies(user).order_by("name").values_list("name", flat=True)
    return list(qs[:limit]) if limit else list(qs)


def active_client_names(user, limit: int | None = MAX_NAMES_FOR_PROMPT) -> list[str]:
    """Client names, capped by default for the OpenAI prompt's context.

    Pass limit=None to resolve a name against the full list (see
    active_company_names).
    """
    from sales.models import Client

    companies = get_user_companies(user)
    qs = (
        Client.objects.filter(company__in=companies)
        .order_by("name")
        .values_list("name", flat=True)
        .distinct()
    )
    return list(qs[:limit]) if limit else list(qs)


def _letters(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _normalize(value))


def _names_mentioned(
    raw: str, candidates: list[str]
) -> tuple[list[str], list[str]]:
    """Pull whole candidate names out of a comma-separated list.

    Legal names contain commas (``S.L.``, ``S.A.``), so splitting the
    string on every comma turns the suffix into its own filter.
    """

    remaining = _normalize(raw)
    matched: list[str] = []
    ordered = sorted(
        candidates, key=lambda item: len(_normalize(item)), reverse=True
    )
    for name in ordered:
        needle = _normalize(name)
        if len(_letters(needle)) < 4 or needle not in remaining:
            continue
        matched.append(name)
        remaining = remaining.replace(needle, " ", 1)

    warnings: list[str] = []
    for part in re.split(r"[,;]|\by\b|\band\b", remaining):
        piece = part.strip(" .")
        if len(_letters(piece)) < 4:
            continue
        match = best_name_match(piece, candidates)
        if match and match not in matched:
            matched.append(match)
            continue
        if match:
            continue
        warnings.append(piece)
    return matched, warnings


def _resolve_names(
    raw_values: list[str], candidates: list[str]
) -> tuple[list[str], list[str]]:
    """Match each raw name against candidates; report the ones that miss."""

    matched: list[str] = []
    warnings: list[str] = []
    for raw in raw_values:
        match = best_name_match(raw, candidates)
        if match:
            if match not in matched:
                matched.append(match)
        else:
            warnings.append(
                _(
                    "“%(name)s” was not found among your active companies/"
                    "clients and was left out of the filters."
                )
                % {"name": raw}
            )
    return matched, warnings


def resolve_spec_names(spec: ReportSpec, user) -> tuple[ReportSpec, list[str]]:
    """
    Rewrite text filters to canonical names in the user's active scope.

    Unmatched client/company name filters are dropped (they would return
    zero rows) and a warning is returned for the UI.
    """
    dataset = get_dataset(spec.dataset)
    filter_map = dataset.filter_map()
    company_names = active_company_names(user, limit=None)
    client_names = active_client_names(user, limit=None)

    resolved: list[ReportFilter] = []
    warnings: list[str] = []

    for filt in spec.filters:
        field = filter_map.get(filt.field)
        if field is None or not field.text_match or not filt.value:
            resolved.append(filt)
            continue

        if filt.op == FilterOperator.IN and filt.field in ("client", "company"):
            candidates = client_names if filt.field == "client" else company_names
            if isinstance(filt.value, list):
                raw_values = [
                    str(part).strip() for part in filt.value if str(part).strip()
                ]
                matched, name_warnings = _resolve_names(raw_values, candidates)
                warnings.extend(name_warnings)
            else:
                matched, missed = _names_mentioned(str(filt.value), candidates)
                for missed_name in missed:
                    warnings.append(
                        _(
                            "“%(name)s” was not found among your active "
                            "companies/clients and was left out of the filters."
                        )
                        % {"name": missed_name}
                    )
            if not matched:
                continue
            if len(matched) == 1:
                resolved.append(
                    replace(filt, value=matched[0], op=FilterOperator.ICONTAINS)
                )
            else:
                resolved.append(
                    replace(filt, value=matched, op=FilterOperator.IN)
                )
            continue

        raw = str(filt.value)

        if filt.field == "client":
            match = best_name_match(raw, client_names)
            if match:
                resolved.append(replace(filt, value=match, op=FilterOperator.ICONTAINS))
                continue
            warnings.append(
                _(
                    "Client “%(name)s” was not found among your clients "
                    "and was left out of the filters."
                )
                % {"name": raw}
            )
            continue

        if filt.field == "company":
            match = best_name_match(raw, company_names)
            if match:
                resolved.append(replace(filt, value=match, op=FilterOperator.ICONTAINS))
                continue
            warnings.append(
                _(
                    "Company “%(name)s” was not found in your active companies "
                    "and was left out of the filters."
                )
                % {"name": raw}
            )
            continue

        resolved.append(replace(filt, op=FilterOperator.ICONTAINS))

    # Drop parameters that no longer have a bound filter after removals.
    remaining_params = {f.parameter for f in resolved if f.parameter}
    parameters = [p for p in spec.parameters if p in remaining_params]
    return (
        ReportSpec(
            dataset=spec.dataset,
            dimensions=list(spec.dimensions),
            aggregations=list(spec.aggregations),
            filters=resolved,
            parameters=parameters,
            chart=spec.chart,
            chart_label_dimension=spec.chart_label_dimension,
            chart_value_aggregation=spec.chart_value_aggregation,
        ),
        warnings,
    )
