import logging

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export.service import ConfigurationExportService

logger = logging.getLogger(__name__)


class PDFService:
    """
    Converts rendered HTML (from the existing ConfigurationExportService)
    into actual PDF bytes using WeasyPrint.
    """

    @staticmethod
    def html_to_pdf(html_string: str) -> bytes:
        """
        Converts an HTML string to PDF bytes.
        """
        try:
            from weasyprint import HTML
        except OSError as exc:
            logger.exception(
                "WeasyPrint native libraries are missing. "
                "Install Pango/Cairo (Linux: system packages; macOS: brew install pango cairo gdk-pixbuf glib)."
            )
            raise RuntimeError(
                "PDF generation is unavailable: WeasyPrint system libraries are not installed."
            ) from exc
        return HTML(string=html_string).write_pdf()

    @staticmethod
    def render_object_pdf(obj, template_kind: str, company=None) -> bytes | None:
        """
        Full pipeline: find the best export template → render HTML → convert to PDF.

        Uses the existing ConfigurationExportTemplate.get_default_for_product()
        and ConfigurationExportService.render_html() to reuse admin-defined
        export templates.

        Args:
            obj: The model instance (SalesInvoice, Quote, Configuration, etc.)
            template_kind: An ExportTemplateKind value (e.g. 'sales_invoice')
            company: Optional company for template resolution

        Returns:
            PDF bytes, or None if no template found.
        """
        strategy = ConfigurationExportService.get_strategy(obj)
        product = strategy.get_product()

        template = ConfigurationExportTemplate.get_default_for_product(
            product=product,
            kind=template_kind,
            company=company,
        )

        if not template:
            logger.warning(
                "No export template of kind '%s' found for %s (pk=%s)",
                template_kind, type(obj).__name__, obj.pk,
            )
            return None

        html = ConfigurationExportService.render_html(obj, template)
        return PDFService.html_to_pdf(html)
