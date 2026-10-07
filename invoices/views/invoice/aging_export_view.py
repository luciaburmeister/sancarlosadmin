from datetime import timedelta
from decimal import Decimal

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from django.http import HttpResponse
from django.utils import timezone
from django.views import View

from invoices.models import Invoice, InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus
from core.admin.company_access import get_user_companies


# ── Styling constants ───────────────────────────────────────────────
HEADER_FONT = Font(bold=True, color="FFFFFF")
BUCKET_FILLS = {
    "0-30":  PatternFill("solid", fgColor="10B981"),  # verde
    "31-60": PatternFill("solid", fgColor="F59E0B"),  # ambar
    "61-90": PatternFill("solid", fgColor="F43F5E"),  # rojo
}


def _set_column_widths(ws, widths):
    """Ajusta el ancho de las columnas por índice (base 1)."""
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width


class AgingExportView(View):
    """
    Exporta los vencimientos de cobros (AR) o pagos (AP) a Excel.
    Parámetros GET:
      - type: 'ar' (cuentas por cobrar) o 'ap' (cuentas por pagar)
      - company: ID de la empresa (opcional, usa la primera si no se indica)
    """
    model_admin = None

    def get(self, request):
        # ── Resolver empresa ───────────────────────────────────────
        user_companies = get_user_companies(request.user)
        company_id = request.GET.get("company")

        try:
            company_id = int(company_id) if company_id else None
        except (ValueError, TypeError):
            company_id = None

        if company_id and user_companies.filter(id=company_id).exists():
            company = user_companies.get(id=company_id)
        elif user_companies.exists():
            company = user_companies.first()
        else:
            return HttpResponse("No hay empresa disponible.", status=400)

        export_type = request.GET.get("type", "ar")

        if export_type == "ar":
            return self._export_ar(company)
        else:
            return self._export_ap(company)

    def _export_ar(self, company):
        """Cuentas por cobrar — facturas de venta pendientes agrupadas por vencimiento."""
        today = timezone.localdate()

        base_qs = SalesInvoice.objects.filter(
            company=company,
            status=SalesInvoiceStatus.FINAL,
            due_date__isnull=False,
        ).exclude(
            payment_status=InvoicePaymentStatus.PAID,
        ).select_related("client").order_by("due_date")

        buckets = {
            "0-30":  base_qs.filter(due_date__gte=today, due_date__lte=today + timedelta(days=30)),
            "31-60": base_qs.filter(due_date__gte=today + timedelta(days=31), due_date__lte=today + timedelta(days=60)),
            "61-90": base_qs.filter(due_date__gte=today + timedelta(days=61), due_date__lte=today + timedelta(days=90)),
        }

        columns = [
            "Número de Factura",
            "Cliente",
            "Fecha de Emisión",
            "Fecha de Vencimiento",
            "Importe Total",
            "Estado de Pago",
        ]

        def row(inv):
            return [
                inv.display_number,
                str(inv.client) if inv.client else "—",
                inv.date_issued,
                inv.due_date,
                float(inv.total_amount.amount) if inv.total_amount else 0,
                inv.get_payment_status_display(),
            ]

        wb = self._build_workbook(
            buckets=buckets,
            columns=columns,
            row_fn=row,
        )

        return self._to_response(wb, f"antiguedad_cobros_{company.name}_{today}.xlsx")

    def _export_ap(self, company):
        """Cuentas por pagar — facturas de compra pendientes agrupadas por vencimiento."""
        today = timezone.localdate()

        base_qs = Invoice.objects.filter(
            company=company,
            due_date__isnull=False,
        ).exclude(
            payment_status=InvoicePaymentStatus.PAID,
        ).select_related("provider").order_by("due_date")

        buckets = {
            "0-30":  base_qs.filter(due_date__gte=today, due_date__lte=today + timedelta(days=30)),
            "31-60": base_qs.filter(due_date__gte=today + timedelta(days=31), due_date__lte=today + timedelta(days=60)),
            "61-90": base_qs.filter(due_date__gte=today + timedelta(days=61), due_date__lte=today + timedelta(days=90)),
        }

        columns = [
            "Número de Factura",
            "Proveedor",
            "Fecha de Emisión",
            "Fecha de Vencimiento",
            "Importe Total",
            "Estado de Pago",
        ]

        def row(inv):
            return [
                inv.invoice_number or "—",
                str(inv.provider) if inv.provider else "—",
                inv.date_issued,
                inv.due_date,
                float(inv.total_amount.amount) if inv.total_amount else 0,
                inv.get_payment_status_display(),
            ]

        wb = self._build_workbook(
            buckets=buckets,
            columns=columns,
            row_fn=row,
        )

        return self._to_response(wb, f"antiguedad_pagos_{company.name}_{today}.xlsx")

    def _build_workbook(self, buckets, columns, row_fn):
        """
        Construye un libro Excel con una pestaña por cada bucket de vencimiento.
        Row 1 = column headers (coloured). Data starts on row 2.
        """
        wb = openpyxl.Workbook()
        wb.remove(wb.active)  # eliminar la hoja vacía por defecto

        # Tab names have no accents so they open cleanly on all systems
        sheet_names = {
            "0-30":  "0-30 Dias",
            "31-60": "31-60 Dias",
            "61-90": "61-90 Dias",
        }

        for key, qs in buckets.items():
            ws = wb.create_sheet(title=sheet_names[key])

            # Row 1 — column headers
            for col_idx, col_name in enumerate(columns, start=1):
                cell = ws.cell(row=1, column=col_idx, value=col_name)
                cell.font = HEADER_FONT
                cell.fill = BUCKET_FILLS[key]
                cell.alignment = Alignment(horizontal="center")

            # Row 2 onwards — data
            invoices = [row_fn(inv) for inv in qs]
            if not invoices:
                ws.cell(row=2, column=1, value="No hay facturas en este período")
            else:
                for row_idx, inv_row in enumerate(invoices, start=2):
                    for col_idx, value in enumerate(inv_row, start=1):
                        ws.cell(row=row_idx, column=col_idx, value=value)

            _set_column_widths(ws, [20, 30, 15, 15, 15, 15])

        return wb

    @staticmethod
    def _to_response(wb, filename):
        """Convierte el libro en una respuesta HTTP para descarga."""
        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        wb.save(response)
        return response