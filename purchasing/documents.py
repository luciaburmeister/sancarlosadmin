"""Renderers for the purchasing documents downloaded from the admin (see core.documents)."""
from configurator.models import ExportTemplateKind
from core.documents import document_renderer
from core.services.pdf_service import PDFService
from purchasing.models import PurchaseOrder, QuoteRequest


@document_renderer
def render_purchase_order_pdf(purchase_order_id: int) -> bytes | None:
    purchase_order = PurchaseOrder.objects.select_related("company", "provider").get(pk=purchase_order_id)
    return PDFService.render_object_pdf(
        purchase_order, ExportTemplateKind.PURCHASE_ORDER, company=purchase_order.company,
    )


@document_renderer
def render_quote_request_pdf(quote_request_id: int) -> bytes | None:
    quote_request = QuoteRequest.objects.select_related("company").get(pk=quote_request_id)
    return PDFService.render_object_pdf(
        quote_request, ExportTemplateKind.QUOTE_REQUEST, company=quote_request.company,
    )
