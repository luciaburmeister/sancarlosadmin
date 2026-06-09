"""
Verifacti provider implementation.

Maps SalesInvoice data to the Verifacti API format and handles
create, cancel, status, and health-check operations.

API reference: https://www.verifacti.com/docs
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

import requests
from django.contrib.contenttypes.models import ContentType

from apps.einvoicing.providers.base import EInvoicingProvider, SubmissionResult
from django.utils import timezone

if TYPE_CHECKING:
    from apps.einvoicing.models import EInvoicingAccount
    from invoices.models import SalesInvoice

logger = logging.getLogger(__name__)
from invoices.models import AppliedTax

# Verifacti date format: DD-MM-YYYY
_DATE_FMT = "%d-%m-%Y"
_TIMEOUT = 30  # seconds


class VerifactiProvider(EInvoicingProvider):
    """Concrete implementation for the Verifacti API."""

    # ── public interface ─────────────────────────────────────────────────

    def submit_invoice(
        self, account: EInvoicingAccount, invoice: SalesInvoice
    ) -> SubmissionResult:
        payload = self._build_create_payload(account, invoice)
        headers = self._headers(account, idempotency_key=f"create-{invoice.pk}")
        url = f"{account.base_url.rstrip('/')}/verifactu/create"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
            body = resp.json() if resp.content else {}
        except requests.RequestException as exc:
            logger.exception("Verifacti create failed for invoice %s", invoice.pk)
            return SubmissionResult(
                success=False,
                status="error",
                request_payload=payload,
                error_message=str(exc),
            )

        if resp.status_code == 200:
            return SubmissionResult(
                success=True,
                status="accepted",
                provider_record_id=body.get("id", ""),
                request_payload=payload,
                response_payload=body,
            )

        return SubmissionResult(
            success=False,
            status="rejected" if resp.status_code == 400 else "error",
            request_payload=payload,
            response_payload=body,
            error_message=self._extract_error_message(body, resp.status_code),
        )

    def cancel_invoice(
        self, account: EInvoicingAccount, invoice: SalesInvoice
    ) -> SubmissionResult:
        payload = self._build_cancel_payload(account, invoice)
        headers = self._headers(account, idempotency_key=f"cancel-{invoice.pk}")
        url = f"{account.base_url.rstrip('/')}/verifactu/cancel"

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
            body = resp.json() if resp.content else {}
        except requests.RequestException as exc:
            logger.exception("Verifacti cancel failed for invoice %s", invoice.pk)
            return SubmissionResult(
                success=False,
                status="error",
                request_payload=payload,
                error_message=str(exc),
            )

        if resp.status_code == 200:
            return SubmissionResult(
                success=True,
                status="accepted",
                provider_record_id=body.get("id", ""),
                request_payload=payload,
                response_payload=body,
            )

        return SubmissionResult(
            success=False,
            status="rejected" if resp.status_code == 400 else "error",
            request_payload=payload,
            response_payload=body,
            error_message=self._extract_error_message(body, resp.status_code),
        )

    def get_invoice_status(
        self,
        account: EInvoicingAccount,
        serie: str,
        numero: str,
        fecha_expedicion: str,
    ) -> dict:
        url = f"{account.base_url.rstrip('/')}/verifactu/status"
        payload = {
            "serie": serie,
            "numero": numero,
            "fecha_expedicion": fecha_expedicion,
        }
        headers = self._headers(account)
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
            return resp.json() if resp.content else {}
        except requests.RequestException as exc:
            logger.exception("Verifacti status check failed")
            return {"error": str(exc)}

    def check_health(self, account: "EInvoicingAccount") -> bool:
        url = f"{account.base_url.rstrip('/')}/verifactu/health"
        headers = self._headers(account)
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            return resp.status_code == 200
        except requests.RequestException:
            return False

    # ── payload builders ─────────────────────────────────────────────────

    def _build_create_payload(
        self, account: "EInvoicingAccount", invoice: "SalesInvoice"
    ) -> dict:
        """Map a SalesInvoice to the Verifacti /verifactu/create JSON body."""

        lineas = self._build_lineas(invoice)

        payload: dict = {
            "serie": account.invoice_serie,
            "numero": invoice.invoice_number or str(invoice.pk),
            # fecha_expedicion must match the invoice's date_issued.
            # Finalization forces date_issued = today to comply with Veri*Factu.
            "fecha_expedicion": invoice.date_issued.strftime(_DATE_FMT),
            "tipo_factura": "F1",  # Standard invoice
            "descripcion": self._build_description(invoice),
            "lineas": lineas,
            "importe_total": str(invoice.total_amount.amount.quantize(Decimal("0.01"))),
        }

        # Client identification
        client = invoice.client
        if client:
            tax_id = (client.tax_identifier or "").strip()
            if tax_id:
                payload["nif"] = tax_id
            payload["nombre"] = client.name

        return payload

    def _build_cancel_payload(
        self, account: EInvoicingAccount, invoice: SalesInvoice
    ) -> dict:
        return {
            "serie": account.invoice_serie,
            "numero": invoice.invoice_number or str(invoice.pk),
            # fecha_expedicion must match the invoice's date_issued.
            "fecha_expedicion": invoice.date_issued.strftime(_DATE_FMT),
        }

    def _build_lineas(self, invoice: "SalesInvoice") -> list[dict]:
        """Build the 'lineas' array from invoice items and their taxes."""

        ct = ContentType.objects.get_for_model(invoice)
        taxes = list(
            AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
            .select_related("tax_type")
            .order_by("order")
        )

        lineas = []

        for item in invoice.items.all():
            line_total = item.line_total.amount.quantize(Decimal("0.01"))

            linea: dict = {
                "descripcion": item.description[:250],
                "cantidad": str(item.quantity),
                "precio": str(item.unit_price.amount.quantize(Decimal("0.01"))),
                "importe": str(line_total),
                "base_imponible": str(line_total),
                "calificacion_operacion": "S1",  # Subject to tax, not exempt
            }

            # Add the primary tax rate at the line level (required when S1)
            if taxes:
                primary_tax = taxes[0]
                pct = primary_tax.selected_percentage
                cuota = (line_total * pct / Decimal("100")).quantize(Decimal("0.01"))
                linea["tipo_impositivo"] = str(pct)
                linea["cuota_repercutida"] = str(cuota)

            # Attach full tax breakdown
            impuestos = []
            for tax in taxes:
                pct = tax.selected_percentage
                base = line_total
                cuota = (base * pct / Decimal("100")).quantize(Decimal("0.01"))
                impuesto = {
                    "clave_impuesto": self._map_tax_key(tax.tax_type.tax_name),
                    "tipo_impositivo": str(pct),
                    "base_imponible": str(base),
                    "cuota": str(cuota),
                }
                impuestos.append(impuesto)

            if impuestos:
                linea["impuestos"] = impuestos

            lineas.append(linea)

        return lineas

    def _build_description(self, invoice: SalesInvoice) -> str:
        """Build a summary description from the invoice items."""
        items = invoice.items.all()[:5]
        parts = [item.description[:80] for item in items]
        desc = " | ".join(parts)
        if invoice.items.count() > 5:
            desc += " | ..."
        return desc[:250] if desc else f"Factura {invoice.invoice_number}"

    @staticmethod
    def _map_tax_key(tax_name: str) -> str:
        """Map internal tax names to Verifactu clave_impuesto codes."""
        name = tax_name.upper().strip()
        if "IVA" in name:
            return "01"  # IVA
        if "IGIC" in name:
            return "02"  # IGIC
        if "IPSI" in name:
            return "03"  # IPSI
        return "01"  # Default to IVA

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _headers(
        account: EInvoicingAccount, idempotency_key: str | None = None
    ) -> dict:
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {account.get_api_key()}",
        }
        if idempotency_key:
            h["Idempotency-Key"] = f"{account.pk}-{idempotency_key}-{uuid.uuid4().hex[:8]}"
        return h

    @staticmethod
    def _extract_error_message(body: dict, status_code: int) -> str:
        """Extract a comprehensive error message from the Verifacti response.

        The AEAT rejection details may come in several fields depending on the
        error type. We collect everything available into a single string.
        """
        parts: list[str] = []

        # Top-level message
        if body.get("message"):
            parts.append(str(body["message"]))

        # 'errors' — can be a list of strings or list of dicts
        errors = body.get("errors") or body.get("errores")
        if isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict):
                    # e.g. {"codigo": "1234", "descripcion": "NIF incorrecto"}
                    desc = err.get("descripcion") or err.get("description") or err.get("message", "")
                    code = err.get("codigo") or err.get("code", "")
                    if code and desc:
                        parts.append(f"[{code}] {desc}")
                    elif desc:
                        parts.append(str(desc))
                    else:
                        parts.append(str(err))
                else:
                    parts.append(str(err))
        elif isinstance(errors, str):
            parts.append(errors)

        # AEAT-specific fields
        for key in ("csv_estado", "estado", "detalle", "descripcion_error",
                     "resultado", "codigo_error", "error"):
            val = body.get(key)
            if val:
                parts.append(f"{key}: {val}")

        if not parts:
            parts.append(f"HTTP {status_code}")

        return " | ".join(parts)
