"""Save an aggregate the chat already ran as a report the informes module can reopen."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _

from apps.ai_agent.models import AIMessage
from apps.reports.constants import ReportDomain, ReportVisibility
from apps.reports.models import Report
from apps.reports.services.create_report import ReportCreationService
from apps.reports.spec import ReportSpec, ReportSpecError
from company.models import Company
from core.admin.company_access import get_user_allowed_companies


class SaveReportError(Exception):
    """The reply cannot be stored as a report."""


def _aggregate_input(message: AIMessage) -> dict:
    for call in reversed(message.tool_calls or []):
        if call.get("name") == "aggregate" and isinstance(call.get("input"), dict):
            return call["input"]
    raise SaveReportError(_("This reply has no report to save."))


def _spec_from_input(tool_input: dict) -> ReportSpec:
    filters = []
    for raw in tool_input.get("filters") or []:
        if not isinstance(raw, dict):
            continue
        filters.append(
            {
                "field": raw.get("field"),
                "op": raw.get("op"),
                "value": raw.get("value"),
                "parameter": None,
            }
        )
    try:
        return ReportSpec.from_dict(
            {
                "dataset": tool_input.get("dataset"),
                "dimensions": tool_input.get("dimensions") or [],
                "aggregations": tool_input.get("aggregations") or [],
                "filters": filters,
                "parameters": [],
                "chart": tool_input.get("chart") or "none",
                "chart_label_dimension": tool_input.get("chart_label_dimension"),
                "chart_value_aggregation": tool_input.get("chart_value_aggregation"),
            }
        )
    except ReportSpecError as exc:
        raise SaveReportError(str(exc)) from exc


def _named_companies(allowed: list[Company], spec: ReportSpec) -> list[Company]:
    needles = []
    for filt in spec.filters:
        if filt.field != "company" or filt.value in (None, ""):
            continue
        if isinstance(filt.value, (list, tuple)):
            needles.extend(str(item) for item in filt.value if item)
        else:
            needles.append(str(filt.value))
    if not needles:
        return list(allowed)

    matched = []
    seen = set()
    for company in allowed:
        haystack = company.name.casefold()
        for needle in needles:
            token = needle.casefold()
            if token and (token in haystack or haystack in token):
                if company.pk not in seen:
                    matched.append(company)
                    seen.add(company.pk)
                break
    if not matched:
        raise SaveReportError(
            _("No company in that report matches one of yours.")
        )
    return matched


def save_report_from_message(
    *, user, message: AIMessage, name: str, visibility: str, prompt: str
) -> Report:
    if not user.has_perm("reports.use_reports"):
        raise PermissionDenied
    if message.conversation.owner_id != user.pk:
        raise PermissionDenied
    if message.role != AIMessage.Role.ASSISTANT:
        raise SaveReportError(_("This reply has no report to save."))

    name = (name or "").strip()
    if not name:
        raise SaveReportError(_("Name the report."))
    if visibility not in ReportVisibility.values:
        raise SaveReportError(_("Choose private or company visibility."))

    allowed = list(get_user_allowed_companies(user))
    if not allowed:
        raise SaveReportError(_("No active company available."))

    spec = _spec_from_input(_aggregate_input(message))
    chosen = sorted(
        _named_companies(allowed, spec), key=lambda item: (item.name, item.pk)
    )
    spec = ReportCreationService.scope_to_companies(spec, chosen)
    domain = spec.dataset if spec.dataset in ReportDomain.values else ""
    report = Report.objects.create(
        company=min(chosen, key=lambda item: item.pk),
        owner=user,
        name=name[:200],
        prompt=(prompt or name)[:4000],
        domain=domain,
        spec=spec.to_dict(),
        visibility=visibility,
    )
    report.included_companies.set(chosen)
    return report
