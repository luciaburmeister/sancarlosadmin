"""
The export wrapper must produce paper-ready HTML: shared @page setup and
self-hosted fonts before the template CSS, container overrides after it, and
no render-time dependency on Google Fonts.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from configurator.services.export.helpers import strip_css_wrappers, strip_hosted_font_imports
from configurator.services.export.service import ConfigurationExportService
from core.services.pdf_service import PDF_BASE_URL, PDFService


class WrapPartsTest(SimpleTestCase):
    def test_print_base_wraps_template_css_in_order(self):
        page = ConfigurationExportService.wrap_parts("<div>body</div>", ".doc-container { max-width: 800px; }")

        self.assertIn("@page", page)
        self.assertIn("size: A4", page)
        self.assertIn("counter(page)", page)
        self.assertIn("/static/exports/fonts/Inter-Regular.ttf", page)
        self.assertIn("/static/exports/fonts/Inter-Bold.ttf", page)

        defaults_at = page.index("@page")
        template_at = page.index(".doc-container { max-width: 800px; }")
        overrides_at = page.index("max-width: none !important")
        self.assertLess(defaults_at, template_at)
        self.assertLess(template_at, overrides_at)

    def test_body_and_context_are_included(self):
        page = ConfigurationExportService.wrap_parts("<p>hello</p>", "", '{"a": 1}')
        self.assertIn("<p>hello</p>", page)
        self.assertIn('id="context-json"', page)
        self.assertIn('{"a": 1}', page)


class StripHostedFontImportsTest(SimpleTestCase):
    def test_inter_google_import_is_removed(self):
        css = (
            "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');\n"
            "body { font-family: 'Inter', sans-serif; }"
        )
        cleaned = strip_hosted_font_imports(css)
        self.assertNotIn("fonts.googleapis.com", cleaned)
        self.assertIn("font-family: 'Inter'", cleaned)

    def test_other_family_imports_are_kept(self):
        css = "@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400&display=swap');"
        self.assertEqual(strip_hosted_font_imports(css), css)

    def test_strip_css_wrappers_applies_it(self):
        css = "<style>@import url(https://fonts.googleapis.com/css2?family=Inter); h1 { color: red; }</style>"
        cleaned = strip_css_wrappers(css)
        self.assertNotIn("googleapis", cleaned)
        self.assertIn("h1 { color: red; }", cleaned)


class UrlFetcherTest(SimpleTestCase):
    def test_static_paths_are_served_from_django_static_files(self):
        result = PDFService.url_fetcher(PDF_BASE_URL + "static/exports/fonts/Inter-Regular.ttf")
        self.assertEqual(result["mime_type"], "font/ttf")
        # TrueType files start with the 0x00010000 scaler type.
        self.assertEqual(result["string"][:4], b"\x00\x01\x00\x00")

    def test_missing_internal_asset_raises(self):
        with self.assertRaises(ValueError):
            PDFService.url_fetcher(PDF_BASE_URL + "static/exports/fonts/does-not-exist.ttf")

    def test_external_urls_fall_through_to_default_fetcher(self):
        with patch("weasyprint.default_url_fetcher", return_value={"string": b"x", "mime_type": "image/png"}) as fetcher:
            result = PDFService.url_fetcher("https://bucket.s3.amazonaws.com/logos/a.png")
        fetcher.assert_called_once()
        self.assertEqual(result["string"], b"x")


class RealRenderTest(SimpleTestCase):
    """Runs WeasyPrint for real; skipped where its native libraries are absent."""

    def test_pdf_is_a4_and_uses_self_hosted_inter(self):
        page = ConfigurationExportService.wrap_parts(
            "<div class='doc-container'><h1>Factura</h1><p>Texto</p></div>",
            "@import url('https://fonts.googleapis.com/css2?family=Inter');"
            ".doc-container { max-width: 800px; margin: 40px auto; padding: 40px; }",
        )
        try:
            with patch("weasyprint.default_url_fetcher", side_effect=AssertionError("network fetch attempted")):
                pdf = PDFService.html_to_pdf(page)
                from weasyprint import HTML
                document = HTML(string=page, base_url=PDF_BASE_URL, url_fetcher=PDFService.url_fetcher).render()
        except (RuntimeError, OSError):
            self.skipTest("WeasyPrint native libraries not installed")

        self.assertTrue(pdf.startswith(b"%PDF"))
        # A4 is 210mm x 297mm; WeasyPrint reports page size in CSS px (96dpi).
        self.assertEqual(len(document.pages), 1)
        self.assertAlmostEqual(document.pages[0].width, 210 * 96 / 25.4, delta=1)
        self.assertAlmostEqual(document.pages[0].height, 297 * 96 / 25.4, delta=1)
