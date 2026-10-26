"""Endpoints for ProjectFile (a.k.a. expedientes)."""

import json
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q
from django.http import JsonResponse
from django.utils import timezone

from api._serializers import (
    project_file_can_mark_completed,
    serialize_configuration,
    serialize_project_file,
)
from api._utils import CompanyAPIView, parse_date_or_error
from api.billing.serializers import advance_block_reason
from invoices.models import SalesInvoice, SalesInvoiceDocumentType, SalesInvoiceStatus
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from management.models import ProjectFile, ProjectFileStatus
from production.services.ticket_cancel import cancel_tickets_for_project_file
from sales.models import Client
from sales.services.client_terms import client_terms_status

_MONTHS_ES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


class ProjectFileListCreateView(CompanyAPIView):
    def get(self, request):
        company = self.company
        status_filter = request.GET.get("status", "open")
        search = request.GET.get("search", "").strip()
        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1

        _PAGE_SIZE = 20

        base_qs = ProjectFile.objects.filter(company=company).select_related("client")
        client_id = request.GET.get("client_id")
        if client_id:
            try:
                base_qs = base_qs.filter(client_id=int(client_id))
            except (ValueError, TypeError):
                pass
        if search:
            base_qs = base_qs.filter(
                Q(name__icontains=search)
                | Q(client__name__icontains=search)
                | Q(client__tax_identifier__icontains=search)
            )

        open_qs = base_qs.exclude(status__in=["completed", "cancelled"])
        # An advance alone, or a cancelled invoice, does not make it "invoiced".
        has_invoice = SalesInvoice.objects.filter(
            project_file_id=OuterRef("pk"),
            document_type=SalesInvoiceDocumentType.INVOICE,
        ).exclude(status=SalesInvoiceStatus.CANCELLED)
        in_progress_qs = open_qs.filter(~Exists(has_invoice))
        invoiced_open_qs = open_qs.filter(Exists(has_invoice))

        in_progress_count = in_progress_qs.count()
        invoiced_open_count = invoiced_open_qs.count()
        open_count = in_progress_count + invoiced_open_count
        completed_count = base_qs.filter(status="completed").count()
        cancelled_count = base_qs.filter(status="cancelled").count()

        if status_filter == "open":
            filtered_qs = open_qs
        elif status_filter in ("in_progress", "open_no_invoice"):
            filtered_qs = in_progress_qs
        elif status_filter in ("invoiced", "open_invoiced"):
            filtered_qs = invoiced_open_qs
        elif status_filter == "completed":
            filtered_qs = base_qs.filter(status="completed")
        elif status_filter == "cancelled":
            filtered_qs = base_qs.filter(status="cancelled")
        else:
            filtered_qs = base_qs

        filtered_qs = filtered_qs.prefetch_related(
            "quotes", "sales_invoices",
        ).order_by("-created_at")

        paginator = Paginator(filtered_qs, _PAGE_SIZE)
        page = paginator.get_page(page_num)

        return JsonResponse({
            "results": [
                serialize_project_file(
                    pf, include_total=True, include_can_mark_completed=True,
                )
                for pf in page
            ],
            "count": paginator.count,
            "open_count": open_count,
            "in_progress_count": in_progress_count,
            "invoiced_open_count": invoiced_open_count,
            "completed_count": completed_count,
            "cancelled_count": cancelled_count,
            "page": page_num,
            "has_next": page.has_next(),
        })

    def post(self, request):
        company = self.company
        body = json.loads(request.body)
        client_id = body.get("client_id")
        name = body.get("name", "").strip()

        try:
            client = Client.objects.get(id=client_id, company=company)
        except Client.DoesNotExist:
            return JsonResponse({"error": "Client not found"}, status=404)

        start_date, err = parse_date_or_error(body.get("date"))
        if err:
            return err
        if not name:
            if start_date:
                name = f"{client.name} - {_MONTHS_ES[start_date.month - 1]} {start_date.year}"
            else:
                name = client.name

        project_file = ProjectFile.objects.create(
            company=company, client=client, name=name, start_date=start_date,
            created_by=request.user,
        )
        return JsonResponse(serialize_project_file(project_file), status=201)


class ProjectFileDetailView(CompanyAPIView):
    def get(self, request, project_file_id):
        try:
            pf = ProjectFile.objects.prefetch_related(
                "quotes__configurations__param_values__parameter",
                "quotes__configurations__product",
                "sales_invoices",
                "production_tickets",
            ).get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        data = serialize_project_file(pf)
        data["configurations"] = [
            serialize_configuration(config)
            for quote in pf.quotes.all()
            for config in quote.configurations.all()
        ]
        invoicing_summary = ProjectFileInvoiceService.get_invoiceable_summary(pf)
        advance_block = advance_block_reason(invoicing_summary)
        can_create = (
            invoicing_summary["total_invoiceable"] > 0
            and invoicing_summary["can_invoice"]
            and not advance_block
        )
        block_reason = None
        if invoicing_summary["total_invoiceable"] <= 0:
            block_reason = "Sin presupuestos aceptados"
        elif not invoicing_summary["can_invoice"]:
            block_reason = "Expediente completamente facturado"
        elif advance_block:
            block_reason = advance_block

        client = pf.client
        if client:
            terms = client_terms_status(client=client, company=self.company)
            data["client_needs_terms_signature"] = terms["needs_terms_signature"]
            data["client_terms_outdated"] = terms["terms_outdated"]
        else:
            data["client_needs_terms_signature"] = False
            data["client_terms_outdated"] = False
        accepted_observations = ProjectFileInvoiceService.get_accepted_quote_observations(pf)
        data["invoicing"] = {
            "total_invoiceable": float(invoicing_summary["total_invoiceable"]),
            "total_invoiced": float(invoicing_summary["total_invoiced"]),
            "remaining": float(invoicing_summary["remaining"]),
            "remaining_subtotal": float(invoicing_summary["remaining_subtotal"]),
            "percentage_invoiced": float(invoicing_summary["percentage_invoiced"]),
            # Taxable base of the advance invoices (cancelled ones left out),
            # and the part of it not yet deducted on a goods invoice.
            "advances_invoiced": float(sum(
                (advance["base"] for advance in invoicing_summary["advances"]),
                Decimal("0.00"),
            )),
            "advances_pending": float(invoicing_summary["advances_open_net"]),
            "can_create": can_create,
            "block_reason": block_reason,
            "client_payment_method": (
                client.payment_method if client else "transfer"
            ),
            "quote_observations": accepted_observations,
        }
        data["sales_invoices"] = [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "proforma_number": inv.proforma_number,
                "status": inv.status,
                "document_type": inv.document_type,
                "advance_percentage": (
                    float(inv.advance_percentage)
                    if inv.advance_percentage is not None else None
                ),
                "payment_status": inv.payment_status,
                "payment_method": inv.payment_method,
                "date_issued": inv.date_issued.strftime("%Y-%m-%d") if inv.date_issued else None,
                "total_amount": float(inv.total_amount.amount) if inv.total_amount else 0.0,
                "amount_remaining": float(inv.amount_remaining.amount) if inv.amount_remaining else 0.0,
            }
            for inv in pf.sales_invoices.all()
        ]
        data["production_tickets"] = [
            {
                "id": t.id,
                "status": t.status,
                "last_stage_label": t.last_stage_label,
                "created_at": t.created_at.strftime("%Y-%m-%d %H:%M"),
            }
            for t in pf.production_tickets.all()
        ]
        data["can_mark_completed"] = project_file_can_mark_completed(pf)
        return JsonResponse(data)

    def patch(self, request, project_file_id):
        try:
            pf = ProjectFile.objects.prefetch_related(
                "quotes__configurations",
                "quotes__configurations__param_values__parameter",
                "quotes__configurations__product",
                "sales_invoices",
                "production_tickets",
            ).get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        body = json.loads(request.body)
        if "status" in body:
            new_status = body["status"]
            # Advances left to deduct must be deducted (or cancelled) first
            if (
                new_status in (ProjectFileStatus.CANCELLED, ProjectFileStatus.COMPLETED)
                and pf.status != new_status
                and pf.advance_pending_amount.amount > Decimal("0.01")
            ):
                return JsonResponse(
                    {"error": "El expediente tiene anticipos pendientes de descontar: "
                              "factúralo o anula los anticipos antes."},
                    status=400,
                )
            if new_status == ProjectFileStatus.CANCELLED:
                if pf.status == ProjectFileStatus.CANCELLED:
                    return JsonResponse(
                        {"error": "El expediente ya está cancelado."},
                        status=400,
                    )
                cancel_tickets_for_project_file(pf)
                pf.status = ProjectFileStatus.CANCELLED
                update_fields = ["status"]
                if not pf.end_date:
                    pf.end_date = timezone.localdate()
                    update_fields.append("end_date")
                pf.save(update_fields=update_fields)
            else:
                pf.status = new_status
                pf.save(update_fields=["status"])
        return JsonResponse(serialize_project_file(pf))

    def delete(self, request, project_file_id):
        try:
            pf = ProjectFile.objects.get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Not found"}, status=404)

        # An advance invoice always keeps its expediente
        if pf.has_advance_invoices():
            return JsonResponse(
                {"error": "No se puede eliminar un expediente con facturas de anticipo."},
                status=400,
            )
        pf.delete()
        return JsonResponse({"success": True})
