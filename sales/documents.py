"""Renderers for the sales documents downloaded from the admin (see core.documents)."""
from datetime import date

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from configurator.services.export.commissionist_agent import CommissionistAgentExportStrategy, SettlementScope
from configurator.services.export.commissionist_agent_workbook import AgentReportWorkbook
from core.documents import document_renderer
from core.services.pdf_service import PDFService
from sales.models import CommissionistAgent, DeliveryOrder, Quote


@document_renderer
def render_quote_pdf(quote_id: int) -> bytes | None:
    quote = Quote.objects.select_related("company", "client").get(pk=quote_id)
    return PDFService.render_object_pdf(
        quote, ExportTemplateKind.CONSOLIDATED_QUOTATION, company=quote.company,
    )


@document_renderer
def render_delivery_order_html(delivery_order_id: int, template_id: int) -> str:
    """The albarán as the printable HTML export page (not a PDF)."""
    delivery_order = DeliveryOrder.objects.get(pk=delivery_order_id)
    template = ConfigurationExportTemplate.objects.get(pk=template_id)
    return ConfigurationExportService.render_html(delivery_order, template)


def _agent_report(agent_id, date_from, date_to, settlement_scope, settlement_id):
    """The agent and the context of their report for the chosen period and settlement."""
    agent = CommissionistAgent.objects.select_related("company").get(pk=agent_id)
    settlement = agent.commission_settlements.get(pk=settlement_id) if settlement_id else None
    # The strategy defaults to the last closed month; this is the chosen period.
    strategy = CommissionistAgentExportStrategy(
        agent, date.fromisoformat(date_from), date.fromisoformat(date_to),
        settlement_scope=settlement_scope, settlement=settlement,
    )
    return agent, strategy.get_context()


@document_renderer
def render_agent_report_pdf(
    agent_id: int, template_id: int, date_from: str, date_to: str,
    settlement_scope: str = SettlementScope.PERIOD, settlement_id: int | None = None,
) -> bytes | None:
    agent, context = _agent_report(agent_id, date_from, date_to, settlement_scope, settlement_id)
    return PDFService.render_object_pdf(
        agent,
        ExportTemplateKind.AGENT_REPORT,
        company=agent.company,
        template_id=template_id,
        extra_context=context,
    )


@document_renderer
def render_agent_report_xlsx(
    agent_id: int, template_id: int, date_from: str, date_to: str,
    settlement_scope: str = SettlementScope.PERIOD, settlement_id: int | None = None,
) -> bytes:
    """The Excel version of the agent report the template prints."""
    template = ConfigurationExportTemplate.objects.get(pk=template_id)
    _agent, context = _agent_report(agent_id, date_from, date_to, settlement_scope, settlement_id)
    return AgentReportWorkbook(context, template.html).render()
