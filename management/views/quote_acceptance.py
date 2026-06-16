from django.shortcuts import get_object_or_404, redirect
from django.views.generic.edit import FormView
from django.contrib.admin.views.decorators import staff_member_required
from django.utils.decorators import method_decorator
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from unfold.views import UnfoldModelAdminViewMixin
from management.forms import LinkProjectFileForm
from management.models import ProjectFile
from sales.models import Quote

@method_decorator(staff_member_required, name='dispatch')
class QuoteAcceptanceWizardView(UnfoldModelAdminViewMixin, FormView):
    template_name = "management/quote_acceptance_wizard.html"
    title = _("Accept Quote & Link Expediente")
    form_class = LinkProjectFileForm
    permission_required = ("sales.change_quote",)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quote_id = self.kwargs.get('quote_id')
        quote = get_object_or_404(Quote, pk=quote_id)
        context['quote'] = quote
        context['opts'] = Quote._meta
        return context

    def get_initial(self):
        initial = super().get_initial()
        quote_id = self.kwargs.get('quote_id')
        quote = get_object_or_404(Quote, pk=quote_id)
        initial['new_project_name'] = quote.project_name
        return initial

    def form_valid(self, form):
        quote_id = self.kwargs.get('quote_id')
        quote = get_object_or_404(Quote, pk=quote_id)
        
        action_type = form.cleaned_data["action_type"]
        project_file = None
        
        if action_type == "new":
            project_name = form.cleaned_data.get("new_project_name") or quote.project_name or f"Expediente - {quote.id}"
            project_file = ProjectFile.objects.create(
                name=project_name,
                client=quote.client,
                company=quote.company,
            )
            messages.success(self.request, _("New Expediente created and linked."))
        else:
            project_file = form.cleaned_data["existing_project"]
            messages.success(self.request, _("Quote linked to existing Expediente."))
        
        if project_file:
            quote.project_file = project_file
            quote.status = "accepted"
            quote.save()
        
        return redirect("admin:sales_quote_change", quote.id)
