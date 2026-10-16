"""Renderer for the public asset transaction document (see core.documents)."""
from assets.models import AssetTransaction
from configurator.models import ConfigurationExportTemplate
from configurator.services.export.service import ConfigurationExportService
from configurator.templatetags.formatting import qr
from core.documents import document_renderer
from core.services.pdf_service import PDFService


@document_renderer
def render_asset_transaction_document(transaction_id: int, template_id: int, document_url: str) -> bytes:
    transaction = AssetTransaction.objects.select_related(
        "asset", "asset__asset_type", "asset__client",
        "company", "project_file", "project_file__client",
        "from_zone", "to_zone", "performed_by",
    ).get(pk=transaction_id)
    template = ConfigurationExportTemplate.objects.get(pk=template_id)
    html = ConfigurationExportService.render_html(
        transaction,
        template,
        extra_context={
            "document_url": document_url,
            "document_qr": qr(document_url),
        },
    )
    return PDFService.html_to_pdf(html)
