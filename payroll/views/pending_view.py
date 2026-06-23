"""
Admin view for resolving an unmatched payroll import row (a pending row).

The user picks the ERP employee the row belongs to; the stored payload is
turned into a real ``EmployeePayroll`` and (optionally) the A3 code is saved
on the employee profile so future imports match automatically.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from payroll.forms import PendingRowResolveForm
from payroll.models import PayrollImportPendingRow
from payroll.services.a3_importer import A3PayrollParseError, resolve_pending_row


class PayrollPendingResolveView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Resolve unmatched employee")
    permission_required = ("payroll.add_employeepayroll",)
    template_name = "admin/payroll/resolve_pending.html"

    def _get_pending(self):
        return get_object_or_404(PayrollImportPendingRow, pk=self.kwargs["object_id"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pending = self._get_pending()
        context["pending"] = pending
        context.setdefault(
            "form", PendingRowResolveForm(company=pending.period.company)
        )
        context["changelist_url"] = reverse(
            "admin:payroll_payrollimportpendingrow_changelist"
        )
        return context

    def post(self, request, *args, **kwargs):
        pending = self._get_pending()
        form = PendingRowResolveForm(
            request.POST, company=pending.period.company
        )
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        try:
            payroll = resolve_pending_row(
                pending,
                form.cleaned_data["employee"],
                set_a3_code=form.cleaned_data["set_a3_code"],
            )
        except A3PayrollParseError as exc:
            messages.error(request, str(exc))
            return self.render_to_response(self.get_context_data(form=form))

        messages.success(
            request,
            _("Assigned to %(name)s. The payroll record was created.")
            % {"name": payroll.employee},
        )
        return redirect(
            reverse("admin:payroll_payrollimportpendingrow_changelist")
        )
