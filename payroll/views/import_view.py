"""
Admin view for importing A3 payroll Excel files.

Two-phase flow:
    1. Upload  -> file is stored in a temporary location and ``validate()``
       runs a dry-run; a preview with totals, warnings and errors is shown.
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

from payroll.forms import A3PayrollImportForm
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
        if request.POST.get("action") == "confirm":
            return self._handle_confirm(request)
        return self._handle_upload(request)

    # ── Phase 1: upload + preview ──────────────────────────────

    def _handle_upload(self, request):
        form = A3PayrollImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        upload = form.cleaned_data["file"]
        token = uuid.uuid4().hex
        stored_path = default_storage.save(f"{_TMP_PREFIX}{token}.xlsx", upload)

        try:
            with default_storage.open(stored_path) as fh:
                service = A3PayrollImportService(fh, source_file=upload.name)
                report = service.validate()
        except A3PayrollParseError as exc:
            default_storage.delete(stored_path)
            messages.error(request, str(exc))
            return self.render_to_response(self.get_context_data(form=A3PayrollImportForm()))

        return self.render_to_response(
            self.get_context_data(
                form=A3PayrollImportForm(),
                report=report,
                token=token,
                source_file=upload.name,
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

        try:
            with default_storage.open(stored_path) as fh:
                service = A3PayrollImportService(fh, source_file=source_file)
                period = service.commit()
        except A3PayrollParseError as exc:
            messages.error(request, str(exc))
            return self.render_to_response(self.get_context_data(form=A3PayrollImportForm()))
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
