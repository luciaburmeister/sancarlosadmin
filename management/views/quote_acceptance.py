from typing import Any

from django.http import HttpResponseRedirect
from django.utils.functional import cached_property
from django.shortcuts import get_object_or_404, redirect
from django.views.generic.edit import FormView
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from unfold.views import UnfoldModelAdminViewMixin
from management.forms import LinkProjectFileForm
from core.admin.company_access import get_user_companies
from management.services.quote_acceptance import QuoteAcceptanceService
from sales.models import Quote

@method_decorator(staff_member_required, name='dispatch')
class QuoteAcceptanceWizardView(UnfoldModelAdminViewMixin, FormView):
    template_name = "management/quote_acceptance_wizard.html"
    title = _("Accept Quote & Link Expediente")
    form_class = LinkProjectFileForm
    permission_required = ("sales.change_quote",)

    @cached_property
    def quote(self) -> Quote:
        """The quote to accept, only among the user's companies."""
        return get_object_or_404(
            Quote.objects.filter(company__in=get_user_companies(self.request.user)), pk=self.kwargs.get("quote_id")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        quote = self.quote
        context['quote'] = quote
        context['opts'] = Quote._meta
        context['require_working_zone'] = bool(
            quote.company and quote.company.require_working_zone_on_project_file
        )
        return context

    def get_initial(self) -> dict[str, Any]:
        initial = super().get_initial()
        initial['new_project_name'] = self.quote.project_name
        return initial

    def get_form_kwargs(self) -> dict[str, Any]:
        kwargs = super().get_form_kwargs()
        kwargs['company'] = self.quote.company
        return kwargs

    def form_valid(self, form: LinkProjectFileForm) -> HttpResponseRedirect:
        quote = self.quote

        if form.cleaned_data["action_type"] == "new":
            project_file = QuoteAcceptanceService.accept(
                quote, project_name=form.cleaned_data.get("new_project_name"), created_by=self.request.user,
            )
            working_zones = form.cleaned_data.get("working_zones")
            if working_zones:
                working_zones.update(project_file=project_file)
            messages.success(self.request, _("New Expediente created and linked."))
        else:
            QuoteAcceptanceService.accept(quote, project_file=form.cleaned_data["existing_project"])
            messages.success(self.request, _("Quote linked to existing Expediente."))

        return redirect("admin:sales_quote_change", quote.id)
