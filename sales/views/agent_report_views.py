from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.http import content_disposition_header
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from configurator.services.export.commissionist_agent import CommissionistAgentExportStrategy
from core.services.pdf_service import PDFService
from sales.forms_agent_reports import AgentReportExportForm


class AgentReportExportView(UnfoldModelAdminViewMixin, FormView):
    """Download one of an agent's monthly reports with an agent_report export template."""

    template_name = "configurator/export_form.html"
    form_class = AgentReportExportForm
    # The reports show the agent's quotes, invoices and commissions.
    permission_required = (
        "sales.view_commissionistagent",
        "sales.view_quote",
        "invoices.view_salesinvoice",
        "configurator.view_invoicecommission",
    )
    title = _("Agent reports")

    def dispatch(self, request, *args, **kwargs):
        self.agent = get_object_or_404(
            self.model_admin.get_queryset(request).select_related("company"),
            pk=kwargs.get("agent_id"),
        )
        self.available_templates = ConfigurationExportService.get_available_templates(
            self.agent,
            request.user,
            ExportTemplateKind.AGENT_REPORT,
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["available_templates"] = self.available_templates
        return kwargs

    def form_valid(self, form):
        try:
            template = self.available_templates.get(id=form.cleaned_data["template"])
        except ConfigurationExportTemplate.DoesNotExist:
            raise Http404(_("Template not available for this agent"))

        date_from, date_to = form.get_period()
        pdf_bytes = PDFService.render_object_pdf(
            self.agent,
            ExportTemplateKind.AGENT_REPORT,
            company=self.agent.company,
            template_id=template.pk,
            # The strategy defaults to the last closed month; this is the chosen period.
            extra_context=CommissionistAgentExportStrategy(self.agent, date_from, date_to).get_context(),
        )
        filename = f"{template.name}_{self.agent.name}_{date_from:%d-%m-%Y}_{date_to:%d-%m-%Y}.pdf"
        return HttpResponse(
            pdf_bytes,
            content_type="application/pdf",
            headers={"Content-Disposition": content_disposition_header(True, filename)},
        )
