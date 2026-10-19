"""Orchestrate classification, spec generation, and Report persistence.

Classification, spec generation, and prompt suggestions all default to
Claude (``build_classifier()``, ``ClaudeReportSpecGenerator``,
``ClaudeReportPromptSuggester``); the OpenAI spec-generator implementation
stays importable as a one-line rollback.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.reports.constants import (
    SUPPORTED_DOMAINS,
    FilterOperator,
    ReportDomain,
    ReportGenerationStatus,
    ReportVisibility,
)
from apps.reports.datasets import datasets_access_for_user, get_dataset
from apps.reports.models import Report
from apps.reports.services.classification import ReportClassification, build_classifier
from apps.reports.services.entity_resolver import (
    active_client_names,
    active_company_names,
    resolve_spec_names,
)
from apps.reports.services.claude_classifier import ClaudeClassifierError
from apps.reports.services.claude_client import ClaudeAPIKeyMissing
from apps.reports.services.claude_spec import ClaudeReportSpecGenerator, ClaudeSpecError
from apps.reports.services.claude_suggestions import ClaudeReportPromptSuggester
from apps.reports.services.openai_spec import OpenAISpecError
from apps.reports.services.spec_generation import (
    BaseReportSpecGenerator,
    ReportGenerationContext,
    SpecGeneration,
)
from apps.reports.services.summary import describe_spec
from apps.reports.spec import ReportFilter, ReportSpec
from company.models import Company
from core.admin.company_access import get_user_companies


class ReportCreationError(Exception):
    """User-facing failure while creating a report from natural language."""


@dataclass
class ReportCreationResult:
    report: Report | None
    classification: ReportClassification | None
    message: str
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


@dataclass
class GeneratedReportDraft:
    specs: list[ReportSpec]
    name: str
    gaps: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)


class ReportCreationService:
    def __init__(
        self,
        classifier=None,
        spec_generator: BaseReportSpecGenerator | None = None,
        *,
        jev_client=None,
        suggestion_service=None,
    ):
        self.classifier = classifier or jev_client or build_classifier()
        self.spec_generator = spec_generator or ClaudeReportSpecGenerator()
        self.suggestion_service = suggestion_service or ClaudeReportPromptSuggester()

    def _record_company(
        self, chosen: list[Company], current: Company | None = None
    ) -> Company:
        if current is not None and any(item.pk == current.pk for item in chosen):
            return current
        return min(chosen, key=lambda item: item.pk)

    @staticmethod
    def scope_to_companies(spec: ReportSpec, companies: Iterable[Company]) -> ReportSpec:
        """Replace company filters with the companies the user selected."""

        ordered = sorted(companies, key=lambda company: (company.name, company.pk))
        names = [company.name for company in ordered]
        if len(names) == 1:
            company_filter = ReportFilter(
                field="company",
                op=FilterOperator.ICONTAINS,
                value=names[0],
            )
        else:
            company_filter = ReportFilter(
                field="company",
                op=FilterOperator.IN,
                value=names,
            )
        removed = {
            filt.parameter
            for filt in spec.filters
            if filt.field == "company" and filt.parameter
        }
        filters = [filt for filt in spec.filters if filt.field != "company"]
        filters.append(company_filter)
        parameters = [name for name in spec.parameters if name not in removed]
        return replace(spec, filters=filters, parameters=parameters)

    @staticmethod
    def _as_draft(raw) -> SpecGeneration:
        if isinstance(raw, SpecGeneration):
            return raw
        spec, name = raw
        return SpecGeneration(spec=spec, name=name)

    @staticmethod
    def _summary_for(
        specs: list[ReportSpec], narratives: list[str] | None = None
    ) -> str:
        """Prefer the model's per-section summary; fall back to describe_spec."""

        parts: list[str] = []
        narratives = list(narratives or [])
        for index, spec in enumerate(specs):
            narrative = ""
            if index < len(narratives):
                narrative = (narratives[index] or "").strip()
            parts.append(narrative or describe_spec(spec))
        return " ".join(part for part in parts if part)

    def _resolve_companies(
        self,
        user,
        company: Company | None = None,
        companies: Iterable[Company] | None = None,
    ) -> list[Company]:
        """One active company is implicit. Several require an explicit choice."""

        allowed = list(get_user_companies(user))
        allowed_ids = {item.pk for item in allowed}
        if companies is not None:
            chosen = list(companies)
            if not chosen:
                raise ReportCreationError(_("Select a company for this report."))
            if any(item.pk not in allowed_ids for item in chosen):
                raise ReportCreationError(_("That company is not active for you."))
            return sorted(chosen, key=lambda item: (item.name, item.pk))
        if company is not None:
            if company.pk not in allowed_ids:
                raise ReportCreationError(_("That company is not active for you."))
            return [company]
        if len(allowed) == 1:
            return allowed
        if not allowed:
            raise ReportCreationError(_("No active company available."))
        raise ReportCreationError(_("Select a company for this report."))

    def _classify_and_generate(
        self, user, prompt: str, *, api_key: str, company_name: str
    ):
        missing_key = _("Set a Claude API key on %(company)s.") % {
            "company": company_name
        }
        try:
            classification = self.classifier.classify(prompt, api_key=api_key)
        except ClaudeAPIKeyMissing as exc:
            raise ReportCreationError(missing_key) from exc
        except ClaudeClassifierError as exc:
            raise ReportCreationError(
                _("Could not classify the report request: %(error)s")
                % {"error": str(exc)}
            ) from exc

        if not classification.is_clear:
            if classification.domain == ReportDomain.UNSUPPORTED:
                domain_names = ", ".join(
                    str(d.label).lower() for d in ReportDomain if d in SUPPORTED_DOMAINS
                )
                message = _(
                    "That request is outside the supported reporting domains "
                    "(%(domains)s)."
                ) % {"domains": domain_names}
            elif classification.needs_clarification:
                message = _(
                    "The request needs more detail. "
                    "Try naming the metric, grouping, and date range."
                )
            else:
                message = _(
                    "Could not confidently classify the report. "
                    "Try rephrasing with a clearer domain."
                )
            suggestions = self.suggestion_service.suggest(
                prompt, datasets_access_for_user(user), api_key=api_key
            )
            return None, classification, message, [], suggestions

        permitted = []
        for domain in classification.supported_domains:
            dataset = get_dataset(domain)
            if user.has_perm(dataset.permission):
                permitted.append(domain)
        if not permitted:
            denied = classification.supported_domains[0]
            raise PermissionDenied(
                _("You do not have permission to create %(domain)s reports.")
                % {"domain": ReportDomain(denied).label}
            )

        context = ReportGenerationContext(
            today=timezone.localdate(),
            company_names=active_company_names(user),
            client_names=active_client_names(user),
        )
        specs: list[ReportSpec] = []
        summaries: list[str] = []
        warnings: list[str] = []
        gaps: list[str] = []
        name = ""
        try:
            for domain in permitted:
                raw = self.spec_generator.generate(
                    prompt, domain, context=context, api_key=api_key
                )
                draft = self._as_draft(raw)
                if draft.spec is None:
                    if draft.gap:
                        gaps.append(draft.gap)
                    continue
                spec, name_warnings = resolve_spec_names(draft.spec, user)
                warnings.extend(name_warnings)
                specs.append(spec)
                summaries.append((draft.summary or "").strip())
                if not name:
                    name = draft.name
        except ClaudeAPIKeyMissing as exc:
            raise ReportCreationError(missing_key) from exc
        except (ClaudeSpecError, OpenAISpecError) as exc:
            raise ReportCreationError(
                _("Could not generate the report: %(error)s") % {"error": str(exc)}
            ) from exc

        if not specs:
            gap_message = (
                " ".join(gaps) if gaps else _("Could not generate the report.")
            )
            return (
                GeneratedReportDraft(specs=[], name="", gaps=gaps),
                classification,
                gap_message,
                warnings,
                [],
            )

        return (
            GeneratedReportDraft(
                specs=specs,
                name=name or prompt[:200],
                gaps=gaps,
                summaries=summaries,
            ),
            classification,
            _("Report created."),
            warnings + gaps,
            [],
        )

    def apply(self, report: Report) -> None:
        """Fill a saved report from its prompt. The caller persists the row first."""

        user = report.owner
        prompt = (report.prompt or "").strip()
        companies = list(report.included_companies.all())
        if not companies and report.company_id:
            companies = [report.company]

        def fail(message):
            report.status = ReportGenerationStatus.ERROR
            report.generation_error = str(message)
            report.generation_summary = ""
            report.generation_suggestions = []
            report.save(
                update_fields=[
                    "status",
                    "generation_error",
                    "generation_summary",
                    "generation_suggestions",
                    "updated_at",
                ]
            )

        if not user.has_perm("reports.use_reports"):
            fail(_("You do not have permission to create this report."))
            return
        if not prompt:
            fail(_("Describe the report you want to create."))
            return
        if not companies:
            fail(_("Select a company for this report."))
            return

        billing = self._record_company(companies, current=report.company)
        try:
            generated, classification, message, _warnings, suggestions = (
                self._classify_and_generate(
                    user,
                    prompt,
                    api_key=billing.get_claude_api_key(),
                    company_name=billing.name,
                )
            )
        except (ReportCreationError, PermissionDenied) as exc:
            fail(
                str(exc)
                or _("You do not have permission to create this report.")
            )
            return

        if generated is None:
            report.status = ReportGenerationStatus.CLARIFY
            report.generation_error = message
            report.generation_summary = ""
            report.generation_suggestions = suggestions
            report.save(
                update_fields=[
                    "status",
                    "generation_error",
                    "generation_summary",
                    "generation_suggestions",
                    "updated_at",
                ]
            )
            return

        if not generated.specs:
            report.status = ReportGenerationStatus.ERROR
            report.spec = {}
            report.generation_error = (
                " ".join(generated.gaps) if generated.gaps else message
            )
            report.generation_summary = ""
            report.generation_suggestions = []
            report.save(
                update_fields=[
                    "spec",
                    "status",
                    "generation_error",
                    "generation_summary",
                    "generation_suggestions",
                    "updated_at",
                ]
            )
            return

        scoped = [
            self.scope_to_companies(spec, companies) for spec in generated.specs
        ]
        report.company = self._record_company(companies, current=report.company)
        report.name = generated.name
        report.prompt = prompt
        report.domain = scoped[0].dataset
        report.spec = Report.specs_payload(scoped)
        report.status = ReportGenerationStatus.CONFIRM
        report.generation_summary = self._summary_for(
            scoped, narratives=generated.summaries
        )
        report.generation_error = ""
        report.generation_suggestions = []
        report.save(
            update_fields=[
                "company",
                "name",
                "prompt",
                "domain",
                "spec",
                "status",
                "generation_summary",
                "generation_error",
                "generation_suggestions",
                "updated_at",
            ]
        )
        report.included_companies.set(companies)

    def create(
        self,
        *,
        user,
        prompt: str,
        visibility: str = ReportVisibility.PRIVATE,
        company=None,
        companies=None,
    ) -> ReportCreationResult:
        if not user.has_perm("reports.use_reports"):
            raise PermissionDenied

        prompt = (prompt or "").strip()
        if not prompt:
            raise ReportCreationError(_("Describe the report you want to create."))

        chosen = self._resolve_companies(user, company=company, companies=companies)
        billing = self._record_company(chosen)
        generated, classification, message, warnings, suggestions = (
            self._classify_and_generate(
                user,
                prompt,
                api_key=billing.get_claude_api_key(),
                company_name=billing.name,
            )
        )
        if generated is None:
            return ReportCreationResult(
                report=None,
                classification=classification,
                message=message,
                warnings=warnings,
                suggestions=suggestions,
            )

        if not generated.specs:
            raise ReportCreationError(message)
        scoped = [self.scope_to_companies(spec, chosen) for spec in generated.specs]

        report = Report.objects.create(
            company=billing,
            owner=user,
            name=generated.name,
            prompt=prompt,
            domain=scoped[0].dataset,
            spec=Report.specs_payload(scoped),
            visibility=visibility,
            generation_summary=self._summary_for(
                scoped, narratives=generated.summaries
            ),
        )
        report.included_companies.set(chosen)
        return ReportCreationResult(
            report=report,
            classification=classification,
            message=message,
            warnings=warnings,
        )

    def regenerate(
        self,
        report: Report,
        prompt: str,
        user,
        visibility: str | None = None,
        companies=None,
    ) -> ReportCreationResult:
        if report.owner_id != user.pk and not user.is_superuser:
            raise PermissionDenied
        if not user.has_perm("reports.use_reports"):
            raise PermissionDenied

        prompt = (prompt or "").strip()
        if not prompt:
            raise ReportCreationError(_("Describe the report you want to create."))

        if companies is not None:
            chosen = self._resolve_companies(user, companies=companies)
            billing = self._record_company(chosen, current=report.company)
        else:
            chosen = None
            existing = list(report.included_companies.all())
            if not existing and report.company_id:
                existing = [report.company]
            billing = (
                self._record_company(existing, current=report.company)
                if existing
                else report.company
            )
        generated, classification, message, warnings, suggestions = (
            self._classify_and_generate(
                user,
                prompt,
                api_key=billing.get_claude_api_key() if billing else "",
                company_name=billing.name if billing else "",
            )
        )
        if generated is None:
            return ReportCreationResult(
                report=None,
                classification=classification,
                message=message,
                warnings=warnings,
                suggestions=suggestions,
            )

        if not generated.specs:
            raise ReportCreationError(message)
        scoped = list(generated.specs)
        if chosen is not None:
            scoped = [self.scope_to_companies(spec, chosen) for spec in scoped]
            report.company = billing
        report.name = generated.name
        report.prompt = prompt
        report.domain = scoped[0].dataset
        report.spec = Report.specs_payload(scoped)
        report.generation_summary = self._summary_for(
            scoped, narratives=generated.summaries
        )
        update_fields = [
            "name",
            "prompt",
            "domain",
            "spec",
            "generation_summary",
            "updated_at",
        ]
        if companies is not None:
            update_fields.append("company")
        if visibility is not None:
            report.visibility = visibility
            update_fields.append("visibility")
        report.save(update_fields=update_fields)
        if companies is not None:
            report.included_companies.set(chosen)
        return ReportCreationResult(
            report=report,
            classification=classification,
            message=_("Report updated."),
            warnings=warnings,
        )
