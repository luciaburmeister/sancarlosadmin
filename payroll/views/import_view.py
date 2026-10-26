"""
Admin view for importing A3 payroll Excel files.

Two-phase flow:
    1. Upload  -> file is stored in a temporary location and ``validate()``
       runs a dry-run; a preview with totals, warnings and errors is shown.
       The upload form can name the company. That choice is used even when
       several companies share the file NIF, as long as the NIF matches.
       If the company is left empty and the NIF does not resolve to exactly
       one company (missing, unknown, or shared), the preview asks the user
       to pick it and re-validates (``action=revalidate``) — the stored file
       is reused, no re-upload needed.
    2. Confirm -> the stored file is re-opened and ``commit()`` writes the
       data atomically. The temporary file is always cleaned up afterwards.
"""

import uuid

from django.contrib import messages
from django.core.files.storage import default_storage
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.models import Company
from payroll.forms import A3PayrollImportForm, ImportCompanySelectForm
from payroll.services.a3_importer import A3PayrollImportService, A3PayrollParseError

_TMP_PREFIX = "payroll_imports/"


class PayrollImportAdminView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Import Payroll from A3")
    permission_required = ("payroll.add_employeepayroll",)
    template_name = "admin/payroll/import.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", A3PayrollImportForm())
        context["changelist_url"] = reverse("admin:payroll_payrollperiod_changelist")
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        if action == "confirm":
            return self._handle_confirm(request)
        if action == "revalidate":
            return self._handle_revalidate(request)
        return self._handle_upload(request)

    # ── Phase 1: upload + preview ──────────────────────────────

    def _handle_upload(self, request):
        form = A3PayrollImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        upload = form.cleaned_data["file"]
        company = form.cleaned_data["company"]
        token = uuid.uuid4().hex
        stored_path = default_storage.save(f"{_TMP_PREFIX}{token}.xlsx", upload)

        try:
            report = self._validate_stored(stored_path, upload.name, company=company)
        except A3PayrollParseError as exc:
            default_storage.delete(stored_path)
            messages.error(request, str(exc))
            return self.render_to_response(
                self.get_context_data(form=A3PayrollImportForm())
            )

        return self.render_to_response(
            self._preview_context(
                report,
                token=token,
                source_file=upload.name,
                selected_company_id=company.pk if company else None,
            )
        )

    # ── Phase 1b: re-validate with a manually selected company ─

    def _handle_revalidate(self, request):
        token = request.POST.get("token", "")
        source_file = request.POST.get("source_file", "")
        stored_path = f"{_TMP_PREFIX}{token}.xlsx"

        if not token or not default_storage.exists(stored_path):
            messages.error(request, _("The uploaded file expired. Please upload it again."))
            return redirect(reverse("admin:payroll_payroll_import"))

        company_form = ImportCompanySelectForm(request.POST)
        if not company_form.is_valid():
            return self._render_unresolved_preview(
                stored_path, source_file, token, request.POST
            )
        company = company_form.cleaned_data["company"]

        try:
            report = self._validate_stored(
                stored_path,
                source_file,
                company=company,
                allow_nif_mismatch=True,
            )
        except A3PayrollParseError as exc:
            default_storage.delete(stored_path)
            messages.error(request, str(exc))
            return self.render_to_response(
                self.get_context_data(form=A3PayrollImportForm())
            )

        return self.render_to_response(
            self._preview_context(
                report,
                token=token,
                source_file=source_file,
                selected_company_id=company.pk,
            )
        )

    # ── Phase 2: confirm + commit ──────────────────────────────

    def _handle_confirm(self, request):
        token = request.POST.get("token", "")
        source_file = request.POST.get("source_file", "")
        stored_path = f"{_TMP_PREFIX}{token}.xlsx"

        if not token or not default_storage.exists(stored_path):
            messages.error(request, _("The uploaded file expired. Please upload it again."))
            return redirect(reverse("admin:payroll_payroll_import"))

        company = None
        company_id = request.POST.get("company_id")
        if company_id:
            company = Company.objects.filter(pk=company_id).first()
            if company is None:
                messages.error(request, _("Select a valid company."))
                return redirect(reverse("admin:payroll_payroll_import"))

        try:
            with default_storage.open(stored_path) as fh:
                service = A3PayrollImportService(
                    fh,
                    company=company,
                    source_file=source_file,
                    allow_nif_mismatch=company is not None,
                )
                period = service.commit()
        except A3PayrollParseError as exc:
            messages.error(request, str(exc))
            return self.render_to_response(
                self.get_context_data(form=A3PayrollImportForm())
            )
        finally:
            default_storage.delete(stored_path)

        messages.success(
            request,
            _("Imported payroll for %(company)s — %(m)02d/%(y)d (%(n)d employees).")
            % {
                "company": period.company.name,
                "m": period.month,
                "y": period.year,
                "n": period.payrolls.count(),
            },
        )
        return redirect(reverse("admin:payroll_payrollperiod_change", args=[period.pk]))

    def _validate_stored(
        self, stored_path, source_file, company=None, allow_nif_mismatch=False
    ):
        with default_storage.open(stored_path) as fh:
            return A3PayrollImportService(
                fh,
                company=company,
                source_file=source_file,
                allow_nif_mismatch=allow_nif_mismatch,
            ).validate()

    def _preview_context(self, report, *, token, source_file, selected_company_id=None):
        context = self.get_context_data(
            form=A3PayrollImportForm(),
            report=report,
            token=token,
            source_file=source_file,
            selected_company_id=selected_company_id,
        )
        if report.needs_company_selection:
            context["company_form"] = ImportCompanySelectForm(
                candidate_ids=report.company_candidate_ids
            )
        return context

    def _render_unresolved_preview(self, stored_path, source_file, token, data):
        try:
            report = self._validate_stored(stored_path, source_file)
        except A3PayrollParseError as exc:
            default_storage.delete(stored_path)
            messages.error(self.request, str(exc))
            return self.render_to_response(
                self.get_context_data(form=A3PayrollImportForm())
            )

        context = self._preview_context(report, token=token, source_file=source_file)
        context["company_form"] = ImportCompanySelectForm(
            data, candidate_ids=report.company_candidate_ids
        )
        context["company_form"].is_valid()
        return self.render_to_response(context)
