from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from accounting.services.setup_check import AccountingSetupCheck


class AccountingSetupView(UnfoldModelAdminViewMixin, TemplateView):
    """Read-only checklist of what a company still needs before its invoices can be journalized."""

    title = _("Accounting setup")
    permission_required = ("company.view_company",)
    template_name = "admin/accounting/setup_check.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        company = get_object_or_404(self.model_admin.get_queryset(self.request), pk=self.kwargs["pk"])
        check = AccountingSetupCheck(company)
        sections = check.run()
        context.update({
            "company": company,
            "sections": sections,
            "ready": check.is_ready(sections),
            "back_url": reverse("admin:company_company_change", args=[company.pk]),
        })
        return context
