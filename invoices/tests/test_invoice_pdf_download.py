"""Tests for invoice PDF ZIP helpers used by the Celery bulk download."""
from django.test import SimpleTestCase

from invoices.services.invoice_pdf_download import InvoicePdfDownloadService


class InvoicePdfDownloadHelpersTests(SimpleTestCase):
    def test_unique_archive_names_dedupes(self):
        entries = InvoicePdfDownloadService.unique_archive_names(
            [
                ("factura_A.pdf", b"1"),
                ("factura_A.pdf", b"2"),
                ("factura_B.pdf", b"3"),
            ]
        )
        self.assertEqual(
            [name for name, _ in entries],
            ["factura_A.pdf", "factura_A_2.pdf", "factura_B.pdf"],
        )

    def test_zip_bytes_are_valid_archive(self):
        zip_bytes = InvoicePdfDownloadService.build_zip_bytes(
            [("a.pdf", b"%PDF"), ("b.pdf", b"%PDF")]
        )
        self.assertTrue(zip_bytes.startswith(b"PK"))
