import logging
import mimetypes
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.files.storage import default_storage

from configurator.models import ConfigurationExportTemplate, ExportTemplateKind
from configurator.services.export.service import ConfigurationExportService

logger = logging.getLogger(__name__)


# Sentinel origin that relative URLs in the rendered HTML (/static/..., /media/...)
# are resolved against. It is never contacted over the network: url_fetcher()
# intercepts it and serves the bytes straight from Django's storages, so the
# result is identical on a web worker, a Celery worker or a developer laptop.
PDF_BASE_URL = "https://pdf.internal/"


class PDFService:
    """
    Converts rendered HTML (from the existing ConfigurationExportService)
    into actual PDF bytes using WeasyPrint.
    """

    @staticmethod
    def _read_static(name: str) -> bytes | None:
        # Hashed name from ManifestStaticFilesStorage in production first,
        # then the source file via finders (development, tests).
        try:
            if staticfiles_storage.exists(name):
                with staticfiles_storage.open(name, "rb") as fh:
                    return fh.read()
        except Exception:
            pass
        path = finders.find(name)
        if path:
            with open(path, "rb") as fh:
                return fh.read()
        return None

    @staticmethod
    def _read_media(name: str) -> bytes | None:
        try:
            if default_storage.exists(name):
                with default_storage.open(name, "rb") as fh:
                    return fh.read()
        except Exception:
            logger.exception("Could not read media file %s for PDF rendering", name)
        return None

    @classmethod
    def url_fetcher(cls, url: str, *args, **kwargs):
        """
        WeasyPrint URL fetcher that resolves STATIC_URL and MEDIA_URL paths
        through Django storages instead of HTTP. Everything else (absolute
        S3 URLs, remote images) falls through to WeasyPrint's default fetcher.
        """
        from weasyprint import default_url_fetcher

        parts = urlsplit(url)
        sentinel = urlsplit(PDF_BASE_URL)
        if parts.netloc == sentinel.netloc:
            path = parts.path
            static_url = "/" + settings.STATIC_URL.strip("/") + "/"
            media_url = "/" + settings.MEDIA_URL.strip("/") + "/"

            content = None
            if path.startswith(static_url):
                content = cls._read_static(path[len(static_url):])
            elif path.startswith(media_url):
                content = cls._read_media(path[len(media_url):])

            if content is None:
                raise ValueError(f"PDF asset not found: {path}")

            mime_type, _ = mimetypes.guess_type(path)
            return {"string": content, "mime_type": mime_type or "application/octet-stream"}

        return default_url_fetcher(url, *args, **kwargs)

    @classmethod
    def html_to_pdf(cls, html_string: str) -> bytes:
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
        return HTML(
            string=html_string,
            base_url=PDF_BASE_URL,
            url_fetcher=cls.url_fetcher,
        ).write_pdf()

    @staticmethod
    def render_object_pdf(
        obj, template_kind: str, company=None, extra_context=None, template_id=None,
        subtype=None,
    ) -> bytes | None:
        """
        Full pipeline: find the best export template → render HTML → convert to PDF.

        Uses the existing ConfigurationExportTemplate.get_default_for_product()
        and ConfigurationExportService.render_html() to reuse admin-defined
        export templates.

        Args:
            obj: The model instance (SalesInvoice, Quote, Configuration, etc.)
            template_kind: An ExportTemplateKind value (e.g. 'sales_invoice')
            company: Optional company for template resolution
            template_id: Optional explicit template to use instead of the
                automatically resolved default (e.g. a user's choice from a picker)
            subtype: Optional ExportTemplateSubtype. Limits automatic resolution
                when several templates share template_kind. Ignored when template_id
                is set.

        Returns:
            PDF bytes, or None if no template found.
        """
        if template_id:
            template = ConfigurationExportTemplate.objects.filter(
                pk=template_id, kind=template_kind, is_active=True,
            ).first()
        else:
            strategy = ConfigurationExportService.get_strategy(obj)
            product = strategy.get_product()
            template = ConfigurationExportTemplate.get_default_for_product(
                product=product,
                kind=template_kind,
                company=company,
                subtype=subtype,
                language=ConfigurationExportService.document_language(obj),
            )

        if not template:
            logger.warning(
                "No export template of kind '%s' found for %s (pk=%s)",
                template_kind, type(obj).__name__, obj.pk,
            )
            return None

        html = ConfigurationExportService.render_html(obj, template, extra_context=extra_context)
        return PDFService.html_to_pdf(html)
