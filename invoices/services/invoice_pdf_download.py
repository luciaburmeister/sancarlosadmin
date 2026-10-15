"""Service to download sales/purchase invoice PDFs (single or ZIP)."""
from __future__ import annotations

import io
import logging
import uuid
import zipfile
from collections.abc import Iterable

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import HttpResponse

from configurator.models import ExportTemplateKind
from core.services.pdf_service import PDFService

logger = logging.getLogger(__name__)

STORAGE_PREFIX = "tmp/invoice_pdf_zips/"


class InvoicePdfDownloadService:
    """
    Builds downloadable PDF (or ZIP) responses for sales and purchase invoices.
    Sales invoices are rendered via WeasyPrint; purchase invoices use the
    uploaded attachment. Bulk ZIPs can be built asynchronously via Celery.
    """

    @staticmethod
    def _safe_filename(value: str, fallback: str) -> str:
        raw = (value or "").strip() or fallback
        cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)
        return cleaned or fallback

    @classmethod
    def sales_invoice_pdf_filename(cls, invoice) -> str:
        doc_number = invoice.invoice_number or f"Proforma_{invoice.pk}"
        return f"factura_{cls._safe_filename(doc_number, str(invoice.pk))}.pdf"

    @classmethod
    def purchase_invoice_filename(cls, invoice) -> str:
        if invoice.file and invoice.file.name:
            base = invoice.file.name.rsplit("/", 1)[-1]
            if base:
                return cls._safe_filename(base, f"factura_compra_{invoice.pk}")
        doc_number = invoice.invoice_number or f"compra_{invoice.pk}"
        return f"factura_compra_{cls._safe_filename(doc_number, str(invoice.pk))}.pdf"

    @staticmethod
    def render_sales_invoice_pdf(invoice) -> bytes | None:
        return PDFService.render_object_pdf(
            invoice,
            ExportTemplateKind.SALES_INVOICE,
            company=invoice.company,
        )

    @staticmethod
    def read_purchase_invoice_file(invoice) -> bytes | None:
        if not invoice.file:
            return None
        try:
            with invoice.file.open("rb") as fh:
                return fh.read()
        except Exception:
            return None

    @classmethod
    def collect_sales_entries(cls, invoice_ids: list[int]) -> tuple[list[tuple[str, bytes]], int]:
        from invoices.models import SalesInvoice

        entries: list[tuple[str, bytes]] = []
        skipped = 0
        qs = SalesInvoice.objects.filter(pk__in=invoice_ids).select_related(
            "company", "client", "configuration"
        )
        # Preserve selection order when possible
        by_id = {invoice.pk: invoice for invoice in qs}
        for pk in invoice_ids:
            invoice = by_id.get(pk)
            if invoice is None:
                skipped += 1
                continue
            pdf_bytes = cls.render_sales_invoice_pdf(invoice)
            if not pdf_bytes:
                skipped += 1
                continue
            entries.append((cls.sales_invoice_pdf_filename(invoice), pdf_bytes))
        return entries, skipped

    @classmethod
    def collect_purchase_entries(cls, invoice_ids: list[int]) -> tuple[list[tuple[str, bytes]], int]:
        from invoices.models import Invoice

        entries: list[tuple[str, bytes]] = []
        skipped = 0
        qs = Invoice.objects.filter(pk__in=invoice_ids)
        by_id = {invoice.pk: invoice for invoice in qs}
        for pk in invoice_ids:
            invoice = by_id.get(pk)
            if invoice is None:
                skipped += 1
                continue
            content = cls.read_purchase_invoice_file(invoice)
            if content is None:
                skipped += 1
                continue
            entries.append((cls.purchase_invoice_filename(invoice), content))
        return entries, skipped

    @staticmethod
    def unique_archive_names(entries: Iterable[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
        """Ensure ZIP entry names are unique when invoice numbers collide."""
        used: dict[str, int] = {}
        result: list[tuple[str, bytes]] = []
        for name, content in entries:
            if name not in used:
                used[name] = 1
                result.append((name, content))
                continue
            used[name] += 1
            stem, dot, ext = name.rpartition(".")
            if not dot:
                stem, ext = name, ""
            else:
                ext = f".{ext}"
            result.append((f"{stem}_{used[name]}{ext}", content))
        return result

    @classmethod
    def build_zip_bytes(cls, entries: list[tuple[str, bytes]]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in cls.unique_archive_names(entries):
                zf.writestr(name, content)
        return buffer.getvalue()

    @classmethod
    def save_zip_to_storage(cls, zip_bytes: bytes, zip_filename: str) -> str:
        """Persist ZIP under a unique temp path; returns the storage path."""
        safe_name = cls._safe_filename(zip_filename, "facturas.zip")
        path = f"{STORAGE_PREFIX}{uuid.uuid4().hex}_{safe_name}"
        return default_storage.save(path, ContentFile(zip_bytes))

    @classmethod
    def build_and_store_zip(
        cls,
        *,
        kind: str,
        invoice_ids: list[int],
        user_id: int | None = None,
    ) -> dict:
        """
        Build a ZIP for the given invoice IDs and store it.

        kind: "sales" | "purchase"
        """
        if kind == "sales":
            entries, skipped = cls.collect_sales_entries(invoice_ids)
            zip_filename = "facturas_venta.zip"
        elif kind == "purchase":
            entries, skipped = cls.collect_purchase_entries(invoice_ids)
            zip_filename = "facturas_compra.zip"
        else:
            raise ValueError(f"Unknown invoice PDF zip kind: {kind}")

        if not entries:
            return {
                "status": "empty",
                "message": "No PDF files could be collected for the selected invoices.",
                "count": 0,
                "skipped": skipped,
                "user_id": user_id,
            }

        zip_bytes = cls.build_zip_bytes(entries)
        storage_path = cls.save_zip_to_storage(zip_bytes, zip_filename)
        logger.info(
            "Stored invoice PDF zip kind=%s path=%s count=%s skipped=%s",
            kind, storage_path, len(entries), skipped,
        )
        return {
            "status": "ok",
            "storage_path": storage_path,
            "filename": zip_filename,
            "content_type": "application/zip",
            "count": len(entries),
            "skipped": skipped,
            "user_id": user_id,
        }

    @staticmethod
    def pdf_attachment_response(
        content: bytes,
        filename: str,
        content_type: str = "application/pdf",
    ) -> HttpResponse:
        response = HttpResponse(content, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
