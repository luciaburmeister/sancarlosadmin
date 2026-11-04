from django.contrib.contenttypes.models import ContentType
from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum
from django.urls import reverse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from apps.public_api.idempotency import idempotent
from apps.public_api.pagination import PublicPagination
from apps.public_api.views import PublicAPIView
from apps.public_api.exceptions import Conflict
from general.email import sender_cc, sender_reply_to
from invoices.models import SalesInvoice, SalesInvoiceStatus
from invoices.services.manual_sales_invoice import DraftLine, LineTax, create_draft_sales_invoice
from invoices.services.sales_invoice_service import SalesInvoiceService
from payments.models import PaymentAllocation
from sales.models import Client

from .serializers import (
    SalesInvoiceCreateSerializer,
    SalesInvoiceDetailSerializer,
    SalesInvoiceFilterSerializer,
    SalesInvoiceFinalizedSerializer,
    SalesInvoiceFinalizeSerializer,
    SalesInvoiceSerializer,
)

TAG = "Sales invoices"

IDEMPOTENCY_KEY_PARAMETER = OpenApiParameter(
    "Idempotency-Key", str, OpenApiParameter.HEADER,
    description="Unique per request (e.g. a UUID). Retries with the same key return the first response.",
)


def _with_paid_total(qs):
    """Annotate ``paid_total`` (sum of payments applied) in one query."""
    paid = (
        PaymentAllocation.objects.filter(
            content_type=ContentType.objects.get_for_model(SalesInvoice), object_id=OuterRef("pk"),
        )
        .values("object_id")
        .annotate(total=Sum("amount_applied"))
        .values("total")
    )
    return qs.annotate(paid_total=Subquery(paid, output_field=DecimalField(max_digits=14, decimal_places=2)))


class SalesInvoiceListCreateView(PublicAPIView):
    required_permissions = {
        "GET": ["invoices.view_salesinvoice"],
        "POST": ["invoices.add_salesinvoice"],
    }

    @extend_schema(
        tags=[TAG],
        summary="List sales invoices",
        parameters=[
            SalesInvoiceFilterSerializer,
            OpenApiParameter("page", int, description="Page number (1-based)."),
            OpenApiParameter("page_size", int, description="Results per page (default 50, max 100)."),
        ],
        responses=SalesInvoiceSerializer(many=True),
    )
    def get(self, request):
        filters = SalesInvoiceFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        f = filters.validated_data

        qs = SalesInvoice.objects.filter(company=self.company).select_related("client")
        for field in ("status", "payment_status", "document_type", "client_id"):
            if field in f:
                qs = qs.filter(**{field: f[field]})
        if "date_from" in f:
            qs = qs.filter(date_issued__gte=f["date_from"])
        if "date_to" in f:
            qs = qs.filter(date_issued__lte=f["date_to"])
        if search := f.get("search", "").strip():
            qs = qs.filter(
                Q(invoice_number__icontains=search)
                | Q(proforma_number__icontains=search)
                | Q(client__name__icontains=search)
                | Q(client__tax_identifier__icontains=search)
            )
        qs = _with_paid_total(qs.order_by("-date_issued", "-id"))

        paginator = PublicPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(SalesInvoiceSerializer(page, many=True).data)

    @extend_schema(
        tags=[TAG],
        summary="Create a draft (pro forma) sales invoice",
        description=(
            "Creates a pro forma invoice from free-text lines. The invoice number is assigned "
            "when it is finalized in the ERP. Send an `Idempotency-Key` header to retry safely."
        ),
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        request=SalesInvoiceCreateSerializer,
        responses={201: SalesInvoiceDetailSerializer},
    )
    @idempotent
    def post(self, request):
        serializer = SalesInvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        client = Client.objects.filter(company=self.company, pk=data["client_id"]).first()
        if client is None:
            raise ValidationError({"client_id": ["Client not found."]})

        invoice = create_draft_sales_invoice(
            company=self.company,
            client=client,
            lines=[
                DraftLine(
                    **{k: v for k, v in line.items() if k != "taxes"},
                    taxes=tuple(LineTax(**tax) for tax in line["taxes"]),
                )
                for line in data["lines"]
            ],
            date_issued=data.get("date_issued"),
            due_date=data.get("due_date"),
            payment_method=data.get("payment_method"),
            notes=data["notes"],
            reference=data["reference"],
        )
        invoice = SalesInvoice.objects.select_related("client").get(pk=invoice.pk)
        return Response(
            SalesInvoiceDetailSerializer(invoice).data,
            status=status.HTTP_201_CREATED,
            headers={"Location": reverse("public_api_v1:sales_invoice_detail", args=[invoice.pk])},
        )


class SalesInvoiceDetailView(PublicAPIView):
    required_permissions = {"GET": ["invoices.view_salesinvoice"]}

    @extend_schema(tags=[TAG], summary="Get a sales invoice", responses=SalesInvoiceDetailSerializer)
    def get(self, request, invoice_id):
        invoice = SalesInvoice.objects.select_related("client").filter(company=self.company, pk=invoice_id).first()
        if invoice is None:
            raise NotFound("Sales invoice not found.")
        return Response(SalesInvoiceDetailSerializer(invoice).data)


class SalesInvoiceFinalizeView(PublicAPIView):
    # Same permission the admin's Finalize action needs
    required_permissions = {"POST": ["invoices.change_salesinvoice"]}

    @extend_schema(
        tags=[TAG],
        summary="Finalize a pro forma sales invoice",
        description=(
            "Issues the invoice: it becomes `final`, gets its invoice number and today's issue date "
            "(an automatic due date moves with it), is booked in the ledger and submitted to the tax "
            "authority. This cannot be undone through the API. Only a `proforma` can be finalized; "
            "anything else is a 409. Send an `Idempotency-Key` header to retry safely."
        ),
        parameters=[IDEMPOTENCY_KEY_PARAMETER],
        request=SalesInvoiceFinalizeSerializer,
        responses={200: SalesInvoiceFinalizedSerializer},
    )
    @idempotent
    def post(self, request, invoice_id):
        options = SalesInvoiceFinalizeSerializer(data=request.data)
        options.is_valid(raise_exception=True)

        invoice = SalesInvoice.objects.filter(company=self.company, pk=invoice_id).first()
        if invoice is None:
            raise NotFound("Sales invoice not found.")
        if invoice.status != SalesInvoiceStatus.PROFORMA:
            raise Conflict(
                f"Only a pro forma invoice can be finalized; this one is {invoice.status}.",
                code="invoice_not_proforma",
            )

        invoice = SalesInvoiceService.finalize_invoice(
            invoice,
            send_email=options.validated_data["send_email"],
            reply_to=sender_reply_to(request.user),
            cc_emails=sender_cc(request.user),
        )
        email_status = SalesInvoiceService.get_finalize_email_status(invoice)

        invoice = SalesInvoice.objects.select_related("client").get(pk=invoice.pk)
        invoice._finalize_email = {
            "queued": email_status["email_queued"],
            "to": email_status.get("client_email"),
            "skip_reason": email_status["email_skip_reason"],
        }
        return Response(SalesInvoiceFinalizedSerializer(invoice).data)
