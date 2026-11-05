from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView
from unfold.views import UnfoldModelAdminViewMixin

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from configurator.services.export.commissionist_agent import SettlementScope
from configurator.services.export.commissionist_agent_workbook import AgentReportWorkbook
from core.documents import enqueue_document
from sales.documents import render_agent_report_pdf, render_agent_report_xlsx
from sales.forms_agent_reports import AgentReportExportForm, AgentReportFormat


class AgentReportExportView(UnfoldModelAdminViewMixin, FormView):
    """Download one of an agent's monthly reports with an agent_report export template, as PDF or Excel."""

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
    RENDERERS = {
        AgentReportFormat.PDF: (render_agent_report_pdf, "application/pdf"),
        AgentReportFormat.XLSX: (render_agent_report_xlsx, AgentReportWorkbook.CONTENT_TYPE),
    }

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
        kwargs["agent"] = self.agent
        return kwargs

    def get_initial(self):
        # A settlement's "Print" action opens this page on that settlement
        initial = super().get_initial()
        settlement_id = self.request.GET.get("settlement", "")
        settlement = (
            self.agent.commission_settlements.confirmed().filter(pk=settlement_id).first()
            if settlement_id.isdigit() else None
        )
        if settlement is not None:
            initial.update(
                settlement_scope=SettlementScope.RECORDED, settlement=settlement, date_to=settlement.settled_until,
            )
        return initial

    def form_valid(self, form):
        try:
            template = self.available_templates.get(id=form.cleaned_data["template"])
        except ConfigurationExportTemplate.DoesNotExist:
            raise Http404(_("Template not available for this agent"))

        file_format = form.cleaned_data["file_format"]
        renderer, content_type = self.RENDERERS[file_format]
        date_from, date_to = form.get_period()
        settlement = form.cleaned_data["settlement"]
        filename = f"{template.name}_{self.agent.name}_{date_from:%d-%m-%Y}_{date_to:%d-%m-%Y}.{file_format}"
        return enqueue_document(
            self.request,
            renderer,
            agent_id=self.agent.pk,
            template_id=template.pk,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
            settlement_scope=form.cleaned_data["settlement_scope"],
            settlement_id=settlement.pk if settlement else None,
            filename=filename,
            content_type=content_type,
            back_url=self.request.path,
        )
