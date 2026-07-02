"""API views for creating and managing SalesInvoices from ProjectFiles."""

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.renderers import JSONRenderer

from api._utils import CompanyAPIView
from api.billing.serializers import (
    BulkEmitCandidateSerializer,
    BulkEmitInvoicesSerializer,
    ProjectFileInvoiceCreateSerializer,
    SalesInvoiceDetailSerializer,
    SalesInvoiceFinalizeSerializer,
    SalesInvoiceListItemSerializer,
    SalesInvoicePaymentCreateSerializer,
    SalesInvoiceSendEmailSerializer,
    annotate_email_log,
    no_client_email_q,
    validation_error_response,
    drf_validation_error_response,
)
from company.models import EmailLog
from configurator.models import ExportTemplateKind
from core.services.pdf_service import PDFService
from invoices.models import SalesInvoice, SalesInvoiceStatus
from invoices.services.sales_invoice_service import SalesInvoiceService
from management.models import ProjectFile


class ProjectFileInvoiceCreateView(CompanyAPIView):
    """POST project-files/<id>/invoices/ — create a SalesInvoice from a ProjectFile."""

    def post(self, request, project_file_id):
        try:
            project_file = ProjectFile.objects.get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Expediente no encontrado."}, status=404)

        serializer = ProjectFileInvoiceCreateSerializer(
            data=request.data,
            context={"project_file": project_file, "company": self.company},
        )
        if not serializer.is_valid():
            return validation_error_response(serializer)

        try:
            invoice = serializer.save()
        except DRFValidationError as exc:
            return drf_validation_error_response(exc)

        return JsonResponse(
            SalesInvoiceDetailSerializer(invoice).data,
            status=201,
        )


class SalesInvoiceDetailView(CompanyAPIView):
    """GET sales-invoices/<id>/ — detail with payments."""

    def get(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.select_related("client").get(
                id=invoice_id, company=self.company
            )
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        return JsonResponse(SalesInvoiceDetailSerializer(invoice).data)


class SalesInvoicePdfView(CompanyAPIView):
    """GET sales-invoices/<id>/pdf/ — same export template as admin View PDF (WeasyPrint)."""

    def perform_content_negotiation(self, request, force=False):
        return (JSONRenderer(), "application/json")

    def get(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.select_related("client", "company").get(
                id=invoice_id, company=self.company
            )
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        show_vehicle = request.GET.get("show_vehicle", "true").lower() != "false"
        pdf_bytes = PDFService.render_object_pdf(
            invoice,
            ExportTemplateKind.SALES_INVOICE,
            company=invoice.company,
            extra_context={"show_vehicle": show_vehicle},
        )
        if not pdf_bytes:
            return JsonResponse(
                {"error": "No se encontró plantilla de factura para este documento."},
                status=404,
            )

        doc_number = invoice.invoice_number or f"Proforma_{invoice.id}"
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in doc_number)
        filename = f"factura_{safe_name}.pdf"

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class SalesInvoiceSendEmailView(CompanyAPIView):
    """POST sales-invoices/<id>/send-email/ — queue invoice PDF email to the client."""

    def post(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.select_related("client").get(
                id=invoice_id, company=self.company
            )
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        serializer = SalesInvoiceSendEmailSerializer(
            data=request.data,
            context={"invoice": invoice},
        )
        if not serializer.is_valid():
            return validation_error_response(serializer)

        try:
            invoice = serializer.save()
        except DRFValidationError as exc:
            if hasattr(serializer, "email_meta"):
                return JsonResponse(
                    {
                        "error": "El cliente no tiene email registrado.",
                        **serializer.email_meta,
                    },
                    status=400,
                )
            return drf_validation_error_response(exc)

        payload = SalesInvoiceDetailSerializer(invoice).data
        payload.update(serializer.email_meta)
        return JsonResponse(payload)


class SalesInvoiceFinalizeView(CompanyAPIView):
    """POST sales-invoices/<id>/finalize/ — convert PROFORMA → FINAL."""

    def post(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.get(id=invoice_id, company=self.company)
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        serializer = SalesInvoiceFinalizeSerializer(
            data=request.data,
            context={"invoice": invoice},
        )
        if not serializer.is_valid():
            return validation_error_response(serializer)

        try:
            invoice = serializer.save()
        except DRFValidationError as exc:
            return drf_validation_error_response(exc)

        payload = SalesInvoiceDetailSerializer(invoice).data
        payload.update(SalesInvoiceService.get_finalize_email_status(invoice))
        return JsonResponse(payload)


class SalesInvoicePaymentView(CompanyAPIView):
    """POST sales-invoices/<id>/payments/ — register an inbound payment."""

    def post(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.get(id=invoice_id, company=self.company)
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        serializer = SalesInvoicePaymentCreateSerializer(
            data=request.data,
            context={"invoice": invoice, "company": self.company},
        )
        if not serializer.is_valid():
            return validation_error_response(serializer)

        try:
            invoice = serializer.save()
        except DRFValidationError as exc:
            return drf_validation_error_response(exc)

        return JsonResponse(SalesInvoiceDetailSerializer(invoice).data, status=201)


class BulkEmitCandidatesView(CompanyAPIView):
    """GET sales-invoices/bulk-emit-candidates/ — open expedientes with invoicing eligibility."""

    def get(self, request):
        search = request.GET.get("search", "").strip()
        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1

        qs = (
            ProjectFile.objects
            .filter(company=self.company)
            .exclude(status__in=["completed", "cancelled"])
            .select_related("client")
            .prefetch_related("quotes__configurations", "quotes__items", "quotes__inventory_items")
            .order_by("-created_at")
        )
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(client__name__icontains=search))

        paginator = Paginator(qs, 30)
        page = paginator.get_page(page_num)
        results = BulkEmitCandidateSerializer(page, many=True).data

        return JsonResponse({
            "results": results,
            "count": paginator.count,
            "has_next": page.has_next(),
            "page": page_num,
        })


class BulkEmitInvoicesView(CompanyAPIView):
    """POST sales-invoices/bulk-emit/ — create and finalize one invoice per ProjectFile at 100%."""

    def post(self, request):
        serializer = BulkEmitInvoicesSerializer(
            data=request.data,
            context={"company": self.company},
        )
        if not serializer.is_valid():
            return validation_error_response(serializer)

        result = serializer.save()
        status_code = result.pop("_status_code", 200)
        return JsonResponse(result, status=status_code)


class SalesInvoiceListView(CompanyAPIView):
    """GET sales-invoices/ — paginated list of all SalesInvoices for the company."""

    def get(self, request):
        qs = (
            SalesInvoice.objects.filter(company=self.company)
            .select_related("client")
            .order_by("-date_issued", "-id")
        )
        qs = annotate_email_log(qs)

        payment_status = request.GET.get("payment_status")
        if payment_status:
            qs = qs.filter(payment_status=payment_status)

        status_filter = request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        email_status_filter = request.GET.get("email_status")
        if email_status_filter:
            qs = qs.filter(status=SalesInvoiceStatus.FINAL)
            if email_status_filter == "no_client_email":
                qs = qs.filter(no_client_email_q())
            elif email_status_filter == "sent":
                qs = qs.filter(_email_log_status=EmailLog.EmailStatus.SUCCESS).exclude(no_client_email_q())
            elif email_status_filter == "error":
                qs = qs.filter(_email_log_status=EmailLog.EmailStatus.FAILED).exclude(no_client_email_q())
            elif email_status_filter == "not_sent":
                qs = qs.filter(_email_log_status__isnull=True).exclude(no_client_email_q())

        search = request.GET.get("search", "").strip()
        if search:
            qs = qs.filter(
                Q(invoice_number__icontains=search) | Q(client__name__icontains=search)
            )

        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1

        paginator = Paginator(qs, 20)
        page = paginator.get_page(page_num)
        results = SalesInvoiceListItemSerializer(page, many=True).data

        return JsonResponse({
            "results": results,
            "count": paginator.count,
            "page": page_num,
            "has_next": page.has_next(),
        })
