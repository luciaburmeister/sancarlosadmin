"""
Verifacti provider implementation.

Maps SalesInvoice data to the Verifacti API format and handles
create, cancel, status, and health-check operations.

API reference: https://www.verifacti.com/docs
"""
from __future__ import annotations

import logging
import re
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

import requests
from django.contrib.contenttypes.models import ContentType

from apps.einvoicing.providers.base import EInvoicingProvider, SubmissionResult
from django.utils import timezone

if TYPE_CHECKING:
    from apps.einvoicing.models import EInvoicingAccount
    from invoices.models import SalesInvoice

logger = logging.getLogger(__name__)
from invoices.models import A3TaxGroup, AppliedTax, TaxType

# Verifacti date format: DD-MM-YYYY
_DATE_FMT = "%d-%m-%Y"
_TIMEOUT = 30  # seconds

_CENT = Decimal("0.01")
_AMOUNT_FIELDS = ("base_imponible", "cuota_repercutida", "cuota_recargo_equivalencia")

# Verifactu 'impuesto' codes (AEAT list L1)
_IMPUESTO_IVA = "01"
_IMPUESTO_IPSI = "02"
_IMPUESTO_IGIC = "03"
# clave_regimen of a sale under the recargo de equivalencia
_CLAVE_REGIMEN_RECARGO = "18"
# Nothing records why part of a base carries no tax (not subject, or exempt
# under which article), so it goes as not subject, the generic N1 (arts. 7,
# 14 and others). To be confirmed in the Verifacti sandbox.
_CALIFICACION_UNTAXED = "N1"
# The recargo de equivalencia of each IVA rate (art. 161 LIVA)
_RECARGO_FOR_IVA_RATE = {
    Decimal("21"): Decimal("5.2"),
    Decimal("10"): Decimal("1.4"),
    Decimal("5"): Decimal("0.62"),
    Decimal("4"): Decimal("0.5"),
    Decimal("2"): Decimal("0.26"),
}


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
            "importe_total": str(self._importe_total(lineas)),
        }

        # A credit note reverses its invoice by differences: negative amounts
        original = invoice.rectifies if invoice.is_credit_note else None
        if original is not None:
            payload["tipo_factura"] = invoice.rectification_code or "R1"
            payload["tipo_rectificativa"] = "I"
            # R1-R4 carry the date of the operation the original invoice billed
            payload["fecha_operacion"] = original.date_issued.strftime(_DATE_FMT)
            payload["facturas_rectificadas"] = [{
                "serie": account.invoice_serie,
                "numero": original.invoice_number or str(original.pk),
                "fecha_expedicion": original.date_issued.strftime(_DATE_FMT),
            }]

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
        """Build the 'lineas' array: the invoice's tax breakdown (the AEAT's
        desglose, at most 12 entries), one entry per tax and rate rather than
        one per item.

        Each entry is an invoice-level additive tax row as stored (IGIC, IPSI,
        or else IVA), so its base_amount and calculated_amount already net out
        the header discount and any advance deductions. The recargo de
        equivalencia goes in the recargo fields of its IVA entry. Retentions
        (IRPF) are never sent: the AEAT does not record them. The part of the
        taxable base that no tax row covers (untaxed lines) is sent as an
        entry of its own.
        """

        ct = ContentType.objects.get_for_model(invoice)
        taxes = (
            AppliedTax.objects.filter(content_type=ct, object_id=invoice.pk)
            .select_related("tax_type")
            .order_by("order", "id")
        )

        entries: dict[tuple, dict] = {}  # (impuesto, %) → entry
        surcharges: dict[Decimal, list] = {}  # recargo % → [base, cuota]
        for tax in taxes:
            if self._is_retention(tax.tax_type):
                continue
            pct = tax.selected_percentage
            base, cuota = self._tax_figures(tax, invoice)
            if self._is_surcharge(tax.tax_type):
                figures = surcharges.setdefault(pct, [Decimal("0.00"), Decimal("0.00")])
                figures[0] += base
                figures[1] += cuota
                continue
            code = self._map_tax_key(tax.tax_type)
            if code == _IMPUESTO_IVA and not self._is_iva(tax.tax_type):
                logger.warning(
                    "SalesInvoice %s: tax %s is neither IVA, IGIC nor IPSI — sent to Verifactu as IVA at %s%%.",
                    invoice.pk,
                    tax.tax_type,
                    pct,
                )
            entry = entries.setdefault((code, pct), {
                "impuesto": code,
                "calificacion_operacion": "S1",  # Subject to tax, not exempt
                "base_imponible": Decimal("0.00"),
                "tipo_impositivo": pct,
                "cuota_repercutida": Decimal("0.00"),
            })
            entry["base_imponible"] += base
            entry["cuota_repercutida"] += cuota

        lineas = list(entries.values())
        for pct, (base, cuota) in surcharges.items():
            self._add_surcharge(invoice, lineas, pct, base, cuota)

        untaxed = invoice.taxable_base.amount - sum(
            (linea["base_imponible"] for linea in lineas), Decimal("0.00")
        )
        # Each tax base adds up per-line rounded amounts: up to a cent per
        # line is rounding, not an untaxed line.
        tolerance = _CENT * max(1, invoice.items.count())
        if abs(untaxed) > tolerance or not lineas:
            lineas.append({
                "calificacion_operacion": _CALIFICACION_UNTAXED,
                "base_imponible": untaxed,
            })

        return [
            {
                key: str((value + 0).quantize(_CENT)) if key in _AMOUNT_FIELDS else str(value)
                for key, value in linea.items()
            }
            for linea in lineas
        ]

    @staticmethod
    def _tax_figures(tax, invoice: "SalesInvoice") -> tuple[Decimal, Decimal]:
        """(base, amount) of an invoice-level tax row as stored. A row with no
        base applies to the whole taxable base, as in recalculate_totals."""
        if tax.base_amount is not None:
            base = tax.base_amount.amount
        else:
            base = invoice.taxable_base.amount
        if tax.calculated_amount is not None:
            return base, tax.calculated_amount.amount
        return base, tax.calculate(base)["tax_amount"]

    def _add_surcharge(
        self, invoice: "SalesInvoice", lineas: list[dict], pct: Decimal, base: Decimal, cuota: Decimal
    ) -> None:
        """Put a recargo de equivalencia in the recargo fields of its IVA entry
        (clave_regimen 18). When it covers only part of that IVA base, that
        part becomes an entry of its own and the rest keeps the general regime."""
        linea = self._surcharge_entry(lineas, pct, base)
        if linea is None:
            logger.warning(
                "SalesInvoice %s: recargo de equivalencia %s%% has no IVA to go with — not sent to Verifactu.",
                invoice.pk,
                pct,
            )
            return
        iva_base = linea["base_imponible"]
        if base * iva_base > 0 and abs(base) < abs(iva_base) - _CENT:
            iva_cuota = (base * linea["tipo_impositivo"] / Decimal("100")).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
            rest = dict(
                linea,
                base_imponible=iva_base - base,
                cuota_repercutida=linea["cuota_repercutida"] - iva_cuota,
            )
            linea["base_imponible"] = base
            linea["cuota_repercutida"] = iva_cuota
            position = next(i for i, other in enumerate(lineas) if other is linea)
            lineas.insert(position + 1, rest)
        linea["clave_regimen"] = _CLAVE_REGIMEN_RECARGO
        linea["tipo_recargo_equivalencia"] = pct
        linea["cuota_recargo_equivalencia"] = cuota

    @staticmethod
    def _surcharge_entry(lineas: list[dict], pct: Decimal, base: Decimal) -> dict | None:
        """The IVA entry a recargo goes with: the IVA rate that recargo belongs
        to, else the entry with the same base, else the largest one."""
        candidates = [
            linea for linea in lineas
            if linea["impuesto"] == _IMPUESTO_IVA and "tipo_recargo_equivalencia" not in linea
        ]
        for linea in candidates:
            if _RECARGO_FOR_IVA_RATE.get(linea["tipo_impositivo"]) == pct:
                return linea
        for linea in candidates:
            if abs(linea["base_imponible"] - base) <= _CENT:
                return linea
        return max(candidates, key=lambda linea: abs(linea["base_imponible"]), default=None)

    @staticmethod
    def _importe_total(lineas: list[dict]) -> Decimal:
        """What the AEAT records as the invoice total: Σ bases + Σ IVA/IGIC/IPSI
        + Σ recargo. A retention (IRPF) is not taken off, so on an invoice
        with one this is not invoice.total_amount."""
        return sum(
            (Decimal(linea[field]) for linea in lineas for field in _AMOUNT_FIELDS if field in linea),
            Decimal("0.00"),
        )

    def _build_description(self, invoice: SalesInvoice) -> str:
        """Build a summary description from the invoice items; a credit
        note's says which invoice it rectifies and why (its notes)."""
        if invoice.is_credit_note and invoice.notes:
            return invoice.notes[:250]
        items = invoice.items.all()[:5]
        parts = [item.description[:80] for item in items]
        desc = " | ".join(parts)
        if invoice.items.count() > 5:
            desc += " | ..."
        return desc[:250] if desc else f"Factura {invoice.invoice_number}"

    @staticmethod
    def _is_retention(tax_type) -> bool:
        """IRPF and any other tax that subtracts: never sent to Verifactu."""
        name = TaxType.normalize_tax_name(tax_type.tax_name)
        return (
            tax_type.behavior == "subtract"
            or tax_type.a3_tax_group == A3TaxGroup.IRPF
            or "IRPF" in name
            or "RETENC" in name
        )

    @staticmethod
    def _is_surcharge(tax_type) -> bool:
        """Recargo de equivalencia: sent in the recargo fields of its IVA entry."""
        name = TaxType.normalize_tax_name(tax_type.tax_name)
        return (
            tax_type.a3_tax_group == A3TaxGroup.RE
            or "RECARGO" in name
            or re.fullmatch(r"RE\d*", name) is not None
        )

    @staticmethod
    def _is_iva(tax_type) -> bool:
        """A tax recognised as IVA by its A3 group or its name."""
        name = TaxType.normalize_tax_name(tax_type.tax_name)
        return tax_type.a3_tax_group == A3TaxGroup.IVA or "IVA" in name or name.startswith("VAT")

    @classmethod
    def _map_tax_key(cls, tax_type) -> str | None:
        """Map a tax to its Verifactu 'impuesto' code (AEAT list L1). None for
        retentions (IRPF) and the recargo de equivalencia, which never get an
        entry of their own."""
        if cls._is_retention(tax_type) or cls._is_surcharge(tax_type):
            return None
        name = TaxType.normalize_tax_name(tax_type.tax_name)
        if "IGIC" in name:
            return _IMPUESTO_IGIC
        if "IPSI" in name:
            return _IMPUESTO_IPSI
        # Anything else additive goes as IVA at its stored rate, as before: the
        # 0 % EXENTO/EXENTA rows (mostly inversión del sujeto pasivo) keep the
        # S1 0 % the AEAT already accepted instead of turning into N1.
        return _IMPUESTO_IVA

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
