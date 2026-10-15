"""Renderers for the sales documents downloaded from the admin (see core.documents)."""
from datetime import date

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export import ConfigurationExportService
from configurator.services.export.commissionist_agent import CommissionistAgentExportStrategy
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


@document_renderer
def render_agent_report_pdf(agent_id: int, template_id: int, date_from: str, date_to: str) -> bytes | None:
    agent = CommissionistAgent.objects.select_related("company").get(pk=agent_id)
    period_from, period_to = date.fromisoformat(date_from), date.fromisoformat(date_to)
    return PDFService.render_object_pdf(
        agent,
        ExportTemplateKind.AGENT_REPORT,
        company=agent.company,
        template_id=template_id,
        # The strategy defaults to the last closed month; this is the chosen period.
        extra_context=CommissionistAgentExportStrategy(agent, period_from, period_to).get_context(),
    )
