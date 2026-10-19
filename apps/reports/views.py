from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.forms import Form
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import NoReverseMatch, reverse
from django.utils.html import format_html
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views import View
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from apps.reports.constants import (
    AggregationFunction,
    ReportGenerationStatus,
    ReportVisibility,
)
from apps.reports.datasets import datasets_access_for_user, get_dataset
from apps.reports.forms import ReportCreateForm, build_parameter_form
from apps.reports.models import Report
from apps.reports.services.compiler import ReportCompiler, ReportPermissionDenied
from apps.reports.services.entity_resolver import resolve_spec_names
from apps.reports.services.excel_export import ExcelWorkbookBuilder
from apps.reports.services.summary import describe_spec
from apps.reports.spec import ReportSpec, ReportSpecError
from apps.reports.tasks import generate_report
from company.active_company import ActiveCompany
from company.models import Company
from core.admin.company_access import (
    get_user_allowed_companies,
    get_user_companies,
)


def _record_company(chosen, current=None):
    if current is not None and any(item.pk == current.pk for item in chosen):
        return current
    return min(chosen, key=lambda item: item.pk)


def _reports_in_companies(companies: Iterable[Company]) -> QuerySet[Report]:
    return Report.objects.filter(
        Q(company__in=companies) | Q(included_companies__in=companies)
    ).distinct()


class ReportPromptAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = ReportCreateForm
    permission_required = ("reports.use_reports",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["dataset_access"] = datasets_access_for_user(self.request.user)
        context["prompt_suggestions"] = getattr(self, "prompt_suggestions", [])
        return context

    def form_valid(self, form):
        companies = self._chosen_companies(form)
        if not companies:
            messages.error(self.request, _("Select a company for this report."))
            return self.form_invalid(form)
        report = self.save_for_generation(form, companies)
        generate_report.delay(report.pk)
        messages.success(
            self.request,
            _(
                "Report saved. Open it to confirm the summary before it is created."
            ),
        )
        return redirect(reverse("admin:reports_report_changelist"))

    def _chosen_companies(self, form):
        selected = form.cleaned_data.get("companies")
        if selected is not None:
            return list(selected)
        return list(get_user_companies(self.request.user))

    def save_for_generation(self, form, companies):
        raise NotImplementedError


class ReportCreateAdminView(ReportPromptAdminView):
    title = gettext_lazy("Create report")
    template_name = "admin/reports/report_create.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["companies"] = get_user_companies(self.request.user)
        return kwargs

    def save_for_generation(self, form, companies):
        prompt = form.cleaned_data["prompt"].strip()
        report = Report.objects.create(
            company=_record_company(companies),
            owner=self.request.user,
            name=prompt[:200],
            prompt=prompt,
            domain="",
            spec={},
            visibility=form.cleaned_data["visibility"],
            status=ReportGenerationStatus.PROCESSING,
        )
        report.included_companies.set(companies)
        return report


class ReportEditAdminView(ReportPromptAdminView):
    title = gettext_lazy("Edit report")
    template_name = "admin/reports/report_edit.html"

    def _ensure_report(self) -> Report:
        if not hasattr(self, "report"):
            self.report = self._get_editable_report(
                self.request, self.kwargs["object_id"]
            )
        return self.report

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["companies"] = get_user_companies(self.request.user)
        return kwargs

    def get_initial(self):
        report = self._ensure_report()
        selected = list(report.included_companies.all())
        if not selected and report.company_id:
            selected = [report.company]
        return {
            "prompt": report.prompt,
            "visibility": report.visibility,
            "companies": selected,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        report = self._ensure_report()
        context["report"] = report
        if report.status == ReportGenerationStatus.CLARIFY:
            context["prompt_suggestions"] = report.generation_suggestions or []
            if report.generation_error and not getattr(
                self, "_clarification_messaged", False
            ):
                messages.warning(self.request, report.generation_error)
                self._clarification_messaged = True
        return context

    def save_for_generation(self, form, companies):
        report = self._ensure_report()
        report.prompt = form.cleaned_data["prompt"].strip()
        report.visibility = form.cleaned_data["visibility"]
        report.company = _record_company(companies, current=report.company)
        report.status = ReportGenerationStatus.PROCESSING
        report.generation_error = ""
        report.generation_summary = ""
        report.generation_suggestions = []
        report.save(
            update_fields=[
                "prompt",
                "visibility",
                "company",
                "status",
                "generation_error",
                "generation_summary",
                "generation_suggestions",
                "updated_at",
            ]
        )
        report.included_companies.set(companies)
        return report

    def _get_editable_report(self, request, object_id) -> Report:
        report = get_object_or_404(
            _reports_in_companies(get_user_companies(request.user)),
            pk=object_id,
        )
        if report.owner_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        return report


class ReportRunAdminView(UnfoldModelAdminViewMixin, FormView):
    form_class = Form
    title = gettext_lazy("Report")
    permission_required = ("reports.use_reports",)
    template_name = "admin/reports/report_detail.html"

    def get(self, request, *args, **kwargs):
        redirect_response = self._load_spec(request)
        if redirect_response is not None:
            return redirect_response
        if getattr(self, "_show_proposal", False):
            self.template_name = "admin/reports/report_confirm.html"
            return self.render_to_response(self._proposal_context())
        return self._render_report(request)

    def _proposal_context(self):
        can_edit = (
            self.report.owner_id == self.request.user.pk
            or self.request.user.is_superuser
        )
        can_generate = (
            self.report.status == ReportGenerationStatus.CONFIRM and can_edit
        )
        summary = (self.report.generation_summary or "").strip()
        if not summary and can_generate and self.report.spec:
            try:
                summary = " ".join(
                    describe_spec(spec) for spec in self.report.sections()
                )
            except ReportSpecError:
                pass
        self.title = self.report.name
        context = super(ReportRunAdminView, self).get_context_data(form=Form())
        context.update(
            {
                "title": self.report.name,
                "report": self.report,
                "summary": summary,
                "gap": "" if can_generate else self.report.generation_error,
                "can_generate": can_generate,
                "can_edit": can_edit,
                "confirm_url": reverse(
                    "admin:reports_report_confirm", args=[self.report.pk]
                ),
                "edit_url": reverse(
                    "admin:reports_report_edit", args=[self.report.pk]
                ),
                "changelist_url": reverse("admin:reports_report_changelist"),
            }
        )
        return context

    def _load_spec(self, request):
        self.report = self._get_report(request, self.kwargs["object_id"])
        pending = self._pending_generation(request)
        if pending is not None:
            return pending
        if self._is_proposal():
            self._show_proposal = True
            return None
        try:
            sections = self.report.sections()
        except ReportSpecError as exc:
            messages.error(request, str(exc))
            return redirect(reverse("admin:reports_report_changelist"))
        if not sections:
            messages.error(request, _("This report has no runnable sections."))
            return redirect(reverse("admin:reports_report_changelist"))
        companies = self._companies_for_run(request)
        self._run_companies = companies
        previous_ids = ActiveCompany.get_current_ids()
        if companies:
            ActiveCompany.set_current_ids([company.pk for company in companies])
        resolved: list[ReportSpec] = []
        runtime_warnings: list[str] = []
        try:
            for spec in sections:
                section, warnings = resolve_spec_names(spec, request.user)
                resolved.append(section)
                runtime_warnings.extend(warnings)
        finally:
            ActiveCompany.set_current_ids(previous_ids)
        self.specs = resolved
        for warning in runtime_warnings:
            messages.warning(request, warning)
        return None

    def _pending_generation(self, request):
        status = self.report.status
        changelist = reverse("admin:reports_report_changelist")
        if status == ReportGenerationStatus.PROCESSING:
            messages.info(
                request,
                _("We're generating this report with AI in the background."),
            )
            return redirect(changelist)
        if status == ReportGenerationStatus.CLARIFY:
            return redirect(
                reverse("admin:reports_report_edit", args=[self.report.pk])
            )
        if (
            status == ReportGenerationStatus.ERROR
            and self.report.spec
            and self.report.generation_error
        ):
            messages.error(request, self.report.generation_error)
        return None

    def _is_proposal(self) -> bool:
        if self.report.status == ReportGenerationStatus.CONFIRM:
            return True
        return (
            self.report.status == ReportGenerationStatus.ERROR and not self.report.spec
        )

    def _render_report(self, request):
        form = self.get_form()
        if form.fields and request.GET and not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        context = self.get_context_data(form=form)
        if request.GET.get("export") == "xlsx" and context.get("sections_data"):
            has_rows = any(
                section["table_data"] and section["table_data"]["rows"]
                for section in context["sections_data"]
            )
            if has_rows:
                return self._export_xlsx(context["sections_data"])
        return self.render_to_response(context)

    def get_form(self, form_class=None):
        data = self.request.GET if self.request.GET else None
        return build_parameter_form(self.specs, data=data)

    def _section_chart_label(self, spec: ReportSpec) -> str:
        if spec.chart_value_aggregation:
            return spec.chart_value_aggregation
        counts = [
            agg.alias
            for agg in spec.aggregations
            if agg.function == AggregationFunction.COUNT
        ]
        if counts:
            return counts[0]
        if spec.aggregations:
            return spec.aggregations[0].alias
        return ""

    def _compile_section(
        self, spec: ReportSpec, parameter_values: dict, *, index: int
    ) -> dict:
        dataset = get_dataset(spec.dataset)
        result = ReportCompiler().compile(
            spec,
            self.request.user,
            parameter_values=parameter_values,
            companies=getattr(self, "_run_companies", None)
            or self._companies_for_run(self.request),
        )
        export_rows = [
            [row.get(col, "") for col in result.columns] for row in result.rows
        ]
        table_data = {
            "headers": list(result.columns),
            "rows": self._build_display_rows(result, dataset, export_rows),
            "export_rows": export_rows,
        }
        chart_note = ""
        if result.chart_is_top and result.chart_labels:
            chart_note = _("Top %(count)s") % {"count": len(result.chart_labels)}
        return {
            "spec": spec,
            "dataset_label": dataset.label,
            "result": result,
            "table_data": table_data,
            "chart_labels": result.chart_labels or [],
            "chart_values": result.chart_values or [],
            "chart_value_label": self._section_chart_label(spec),
            "chart_note": chart_note,
            "chart_canvas_id": f"report-chart-{index}",
            "chart_labels_id": f"report-chart-labels-{index}",
            "chart_values_id": f"report-chart-values-{index}",
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = kwargs.get("form") or self.get_form()
        source = (
            form.cleaned_data if (form.is_bound and form.is_valid()) else form.initial
        )
        parameter_values = {
            key: value
            for key, value in (source or {}).items()
            if value not in (None, "")
        }

        sections_data: list[dict] = []
        error = None
        try:
            for index, spec in enumerate(self.specs):
                sections_data.append(
                    self._compile_section(spec, parameter_values, index=index)
                )
        except ReportPermissionDenied:
            error = _("You do not have permission to view this report's data.")
            sections_data = []
        except ReportSpecError as exc:
            error = str(exc)
            sections_data = []
        except (ValueError, TypeError) as exc:
            error = _(
                "Could not run this report with the saved filters (%(error)s)."
            ) % {"error": exc}
            sections_data = []

        query = self.request.GET.copy()
        query.pop("export", None)
        export_query = query.copy()
        export_query["export"] = "xlsx"
        domain_labels = [section["dataset_label"] for section in sections_data]

        context.update(
            {
                "title": self.report.name,
                "report": self.report,
                "specs": self.specs,
                "sections_data": sections_data,
                "domain_labels": domain_labels,
                "generation_summary": (self.report.generation_summary or "").strip(),
                "run_error": error,
                "parameter_form": form,
                "changelist_url": reverse("admin:reports_report_changelist"),
                "export_url": (
                    f"{self.request.path}?{urlencode(export_query, doseq=True)}"
                ),
                "can_edit": (
                    self.report.owner_id == self.request.user.pk
                    or self.request.user.is_superuser
                ),
                "edit_url": reverse(
                    "admin:reports_report_edit", args=[self.report.pk]
                ),
            }
        )
        return context

    def _build_display_rows(self, result, dataset, export_rows: list[list]) -> list:
        """Wrap each row's first cell in a link to its source admin record.

        Only when the dataset exposes a detail view and that row's
        dimensions identified exactly one record (result.row_pks[i]).
        """

        row_urls = list(getattr(result, "row_urls", None) or [])
        if not row_urls and (not dataset.detail_url_name or not result.row_pks):
            return export_rows

        display_rows = []
        for index, row_values in enumerate(export_rows):
            if not row_values:
                display_rows.append(row_values)
                continue
            url = row_urls[index] if index < len(row_urls) else None
            pk = result.row_pks[index] if index < len(result.row_pks) else None
            if not url and pk is not None and dataset.detail_url_name:
                try:
                    url = reverse(dataset.detail_url_name, args=[pk])
                except NoReverseMatch:
                    url = None
            if not url:
                display_rows.append(row_values)
                continue
            linked_cell = {
                "content": format_html(
                    '<a href="{}" target="_blank" rel="noopener noreferrer" '
                    'class="text-primary-600 dark:text-primary-500 hover:underline '
                    'inline-flex items-center gap-1">{} '
                    '<span class="material-symbols-outlined text-sm!">open_in_new</span></a>',
                    url,
                    row_values[0],
                )
            }
            display_rows.append([linked_cell, *row_values[1:]])
        return display_rows

    def _export_xlsx(self, sections_data: list[dict]) -> HttpResponse:
        builder = ExcelWorkbookBuilder()
        for section in sections_data:
            table_data = section["table_data"]
            headers = table_data["headers"]
            export_rows = table_data.get("export_rows", table_data["rows"])
            builder.add_sheet(
                title=str(section["dataset_label"]),
                columns=headers,
                rows=[dict(zip(headers, row)) for row in export_rows],
            )
        filename = f"report-{self.report.pk}.xlsx"
        response = HttpResponse(
            builder.build(),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def _companies_for_run(self, request):
        """Run the report for the companies it was saved with."""

        included = list(self.report.included_companies.all())
        if not included and self.report.company_id:
            included = [self.report.company]
        allowed_ids = set(
            get_user_allowed_companies(request.user).values_list("pk", flat=True)
        )
        chosen = [company for company in included if company.pk in allowed_ids]
        return chosen or list(get_user_companies(request.user))

    def _get_report(self, request, object_id) -> Report:
        report = get_object_or_404(
            _reports_in_companies(get_user_allowed_companies(request.user)),
            pk=object_id,
        )
        if report.owner_id == request.user.pk:
            return report
        if report.visibility == ReportVisibility.COMPANY:
            return report
        raise PermissionDenied


class ReportConfirmAdminView(UnfoldModelAdminViewMixin, View):
    title = gettext_lazy("Confirm report")
    permission_required = ("reports.use_reports",)

    def post(self, request, object_id):
        report = get_object_or_404(
            _reports_in_companies(get_user_companies(request.user)),
            pk=object_id,
        )
        if report.owner_id != request.user.pk and not request.user.is_superuser:
            raise PermissionDenied
        run_url = reverse("admin:reports_report_run", args=[report.pk])
        if report.status != ReportGenerationStatus.CONFIRM or not report.spec:
            messages.error(request, _("This report is not waiting for confirmation."))
            return redirect(run_url)
        report.status = ReportGenerationStatus.READY
        report.save(update_fields=["status", "updated_at"])
        messages.success(
            request,
            _('Report "%(name)s" created.') % {"name": report.name},
        )
        return redirect(run_url)
