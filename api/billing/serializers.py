"""DRF serializers for project-file / sales-invoice billing APIs."""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import OuterRef, Q, Subquery
from django.http import JsonResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError as DRFValidationError

from company.models import EmailLog
from general.email import sender_cc, sender_reply_to
from invoices.models import SalesInvoice, SalesInvoiceStatus
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.sales_invoice_payment import ALLOWED_SALES_INVOICE_PAYMENT_METHODS
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService
from management.models import ProjectFile
from payments.models import (
    Payment,
    PaymentAllocation,
    PaymentDirection,
    PaymentMethod,
    PaymentStatus,
)
from sales.models import Quote, QuoteStatus
from djmoney.money import Money


def validation_error_response(serializer, status=400):
    """Map DRF validation errors to the API's ``{"error": "..."}`` shape."""
    errors = serializer.errors
    if isinstance(errors, dict):
        for key, value in errors.items():
            if key == "non_field_errors" and value:
                return JsonResponse({"error": str(value[0])}, status=status)
            if isinstance(value, list) and value:
                return JsonResponse({"error": str(value[0])}, status=status)
    return JsonResponse({"error": "Datos inválidos."}, status=status)


def drf_validation_error_response(exc, status=400):
    """Map a raised ``ValidationError`` to the API's ``{"error": "..."}`` shape."""
    detail = exc.detail
    if isinstance(detail, list) and detail:
        return JsonResponse({"error": str(detail[0])}, status=status)
    if isinstance(detail, dict):
        for value in detail.values():
            if isinstance(value, list) and value:
                return JsonResponse({"error": str(value[0])}, status=status)
            return JsonResponse({"error": str(value)}, status=status)
    return JsonResponse({"error": str(detail)}, status=status)


def no_client_email_q():
    return Q(client__isnull=True) | Q(client__email__isnull=True) | Q(client__email="")


def annotate_email_log(qs):
    ct = ContentType.objects.get_for_model(SalesInvoice)
    latest_logs = EmailLog.objects.filter(
        content_type=ct, object_id=OuterRef("pk")
    ).order_by("-sent_at")
    return qs.annotate(
        _email_log_status=Subquery(latest_logs.values("status")[:1]),
        _email_log_sent_at=Subquery(latest_logs.values("sent_at")[:1]),
        _email_log_error=Subquery(latest_logs.values("error_message")[:1]),
        _email_delivery_status=Subquery(latest_logs.values("delivery_status")[:1]),
        _email_delivered_at=Subquery(latest_logs.values("delivered_at")[:1]),
        _email_first_opened_at=Subquery(latest_logs.values("first_opened_at")[:1]),
        _email_last_opened_at=Subquery(latest_logs.values("last_opened_at")[:1]),
        _email_open_count=Subquery(latest_logs.values("open_count")[:1]),
        _email_delivery_error=Subquery(latest_logs.values("delivery_error")[:1]),
    )


def quote_has_content(quote):
    for config in quote.configurations.all():
        if config.subtotal_price and config.subtotal_price.amount > 0:
            return True
    for item in quote.items.all():
        if item.line_total and item.line_total.amount > 0:
            return True
    for inv in quote.inventory_items.all():
        if inv.line_total and inv.line_total.amount > 0:
            return True
    return False


def build_items_from_summary(summary, invoice_mode, percentage, custom_amount, show_vehicle_name=True):
    """Invoice line dicts for the API's percentage/custom invoice modes."""
    ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
        summary, invoice_mode, percentage, custom_amount
    )
    return ProjectFileInvoiceService.build_items_for_ratio(
        summary, ratio, show_vehicle_name=show_vehicle_name,
    )


def advance_block_reason(summary):
    """Why the expediente's goods cannot be invoiced while one of its
    advances is still a pro forma (it must be finalized or deleted first)
    or being cancelled at the AEAT (its answer is awaited), or None when
    there is none."""
    numbers = [
        advance["invoice_number"]
        for advance in summary["advances"]
        if advance["status"] == SalesInvoiceStatus.PROFORMA
    ]
    if numbers:
        return f"Anticipo en proforma ({', '.join(numbers)})"
    numbers = [advance["invoice_number"] for advance in summary["advances"] if advance["cancelling"]]
    return f"Anulación del anticipo en curso en la AEAT ({', '.join(numbers)})" if numbers else None


class SalesInvoiceDetailSerializer(serializers.Serializer):
    """Read serializer for a SalesInvoice with items, payments and email log."""

    def to_representation(self, invoice):
        ct = ContentType.objects.get_for_model(invoice)
        is_final = invoice.status == SalesInvoiceStatus.FINAL
        if not is_final:
            email_log_status = None
            email_sent_at = None
            email_error = None
            email_delivery_status = None
            email_delivered_at = None
            email_first_opened_at = None
            email_last_opened_at = None
            email_open_count = 0
            email_delivery_error = None
        elif hasattr(invoice, "_email_log_status"):
            email_log_status = invoice._email_log_status
            email_sent_at = (
                invoice._email_log_sent_at.isoformat()
                if invoice._email_log_sent_at
                else None
            )
            email_error = invoice._email_log_error
            email_delivery_status = invoice._email_delivery_status
            email_delivered_at = (
                invoice._email_delivered_at.isoformat()
                if invoice._email_delivered_at else None
            )
            email_first_opened_at = (
                invoice._email_first_opened_at.isoformat()
                if invoice._email_first_opened_at else None
            )
            email_last_opened_at = (
                invoice._email_last_opened_at.isoformat()
                if invoice._email_last_opened_at else None
            )
            email_open_count = invoice._email_open_count or 0
            email_delivery_error = invoice._email_delivery_error
        else:
            email_log = (
                EmailLog.objects.filter(content_type=ct, object_id=invoice.pk)
                .order_by("-sent_at")
                .first()
            )
            email_log_status = email_log.status if email_log else None
            email_sent_at = email_log.sent_at.isoformat() if email_log else None
            email_error = email_log.error_message if email_log else None
            email_delivery_status = email_log.delivery_status if email_log else None
            email_delivered_at = (
                email_log.delivered_at.isoformat()
                if email_log and email_log.delivered_at else None
            )
            email_first_opened_at = (
                email_log.first_opened_at.isoformat()
                if email_log and email_log.first_opened_at else None
            )
            email_last_opened_at = (
                email_log.last_opened_at.isoformat()
                if email_log and email_log.last_opened_at else None
            )
            email_open_count = email_log.open_count if email_log else 0
            email_delivery_error = email_log.delivery_error if email_log else None

        allocations = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=invoice.pk)
            .select_related("payment")
            .order_by("payment__payment_date", "id")
        )
        payments = [
            {
                "id": alloc.payment.id,
                "amount": float(alloc.amount_applied.amount),
                "currency": alloc.amount_applied_currency,
                "payment_date": alloc.payment.payment_date.strftime("%Y-%m-%d"),
                "method": alloc.payment.method,
                "reference": alloc.payment.reference,
            }
            for alloc in allocations
        ]
        items = [
            {
                "id": item.id,
                "description": item.description,
                "quote_name": item.quote_name,
                "quantity": float(item.quantity),
                "unit_price": float(item.unit_price.amount) if item.unit_price else 0.0,
                "discount_percentage": float(item.discount_percentage),
                "line_total": float(item.line_total.amount) if item.line_total else 0.0,
                # A deduction line takes part of an advance invoice back off
                "is_advance_deduction": item.advance_line_id is not None,
                "advance_invoice_number": (
                    item.advance_line.invoice.display_number
                    if item.advance_line_id else None
                ),
            }
            for item in invoice.items.select_related("advance_line__invoice")
        ]
        return {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "proforma_number": invoice.proforma_number,
            "status": invoice.status,
            "document_type": invoice.document_type,
            "advance_percentage": (
                float(invoice.advance_percentage)
                if invoice.advance_percentage is not None else None
            ),
            "payment_status": invoice.payment_status,
            "payment_method": invoice.payment_method,
            "payment_method_label": invoice.get_payment_method_display(),
            "date_issued": invoice.date_issued.strftime("%Y-%m-%d") if invoice.date_issued else None,
            "due_date": invoice.due_date.strftime("%Y-%m-%d") if invoice.due_date else None,
            "payment_commitment_date": invoice.payment_commitment_date.strftime("%Y-%m-%d") if invoice.payment_commitment_date else None,
            "subtotal_amount": float(invoice.subtotal_amount.amount) if invoice.subtotal_amount else 0.0,
            "discount_amount": float(invoice.discount_amount.amount) if invoice.discount_amount else 0.0,
            "taxable_base": float(invoice.taxable_base.amount),
            "tax_amount": float(invoice.tax_amount.amount) if invoice.tax_amount else 0.0,
            "total_amount": float(invoice.total_amount.amount) if invoice.total_amount else 0.0,
            "amount_paid": float(invoice.amount_paid.amount) if invoice.amount_paid else 0.0,
            "amount_remaining": float(invoice.amount_remaining.amount) if invoice.amount_remaining else 0.0,
            "notes": invoice.notes or "",
            "client_id": invoice.client_id,
            "client_name": invoice.client.name if invoice.client else None,
            "client_email": (invoice.client.email or "").strip() or None if invoice.client else None,
            "project_file_id": invoice.project_file_id,
            "items": items,
            "payments": payments,
            "email_log_status": email_log_status if is_final else None,
            "email_sent_at": email_sent_at,
            "email_error": email_error,
            "email_delivery_status": email_delivery_status,
            "email_delivered_at": email_delivered_at,
            "email_first_opened_at": email_first_opened_at,
            "email_last_opened_at": email_last_opened_at,
            "email_open_count": email_open_count,
            "email_delivery_error": email_delivery_error,
        }


class SalesInvoiceListItemSerializer(serializers.Serializer):
    def to_representation(self, invoice):
        return {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "proforma_number": invoice.proforma_number,
            "status": invoice.status,
            "document_type": invoice.document_type,
            "advance_percentage": (
                float(invoice.advance_percentage)
                if invoice.advance_percentage is not None else None
            ),
            "payment_status": invoice.payment_status,
            "date_issued": invoice.date_issued.strftime("%Y-%m-%d") if invoice.date_issued else None,
            "total_amount": float(invoice.total_amount.amount) if invoice.total_amount else 0.0,
            "amount_remaining": float(invoice.amount_remaining.amount) if invoice.amount_remaining else 0.0,
            "client_name": invoice.client.name if invoice.client else None,
            "client_has_email": bool(invoice.client and (invoice.client.email or "").strip()),
            "project_file_id": invoice.project_file_id,
            "email_log_status": (
                invoice._email_log_status
                if invoice.status == SalesInvoiceStatus.FINAL
                else None
            ),
            "email_delivery_status": (
                invoice._email_delivery_status
                if invoice.status == SalesInvoiceStatus.FINAL
                else None
            ),
            "email_first_opened_at": (
                invoice._email_first_opened_at.isoformat()
                if invoice.status == SalesInvoiceStatus.FINAL
                and invoice._email_first_opened_at
                else None
            ),
        }


class BulkEmitCandidateSerializer(serializers.Serializer):
    def to_representation(self, pf):
        all_quotes = list(pf.quotes.all())
        accepted = [
            q for q in all_quotes
            if q.status == QuoteStatus.ACCEPTED and quote_has_content(q)
        ]
        pending = [
            q for q in all_quotes
            if q.status in (QuoteStatus.DRAFT, QuoteStatus.SENT) and quote_has_content(q)
        ]
        has_content = bool(accepted or pending)
        # Net (pre-tax) goods figures: an advance does not make it invoiced.
        # Also with pending quotes only: bulk-emit accepts them, and a pro
        # forma advance (or one being cancelled) blocks it all the same.
        summary = ProjectFileInvoiceService.get_invoiceable_summary(pf) if has_content else None
        accepted_remaining = (
            max(0.0, float(summary["goods_remaining_net"])) if accepted else 0.0
        )
        advances_open_net = float(summary["advances_open_net"]) if summary else 0.0
        already_invoiced = (
            has_content and accepted and not summary["can_invoice"] and not pending
        )
        advance_block = advance_block_reason(summary) if summary else None
        pending_value = sum(float(q.total_price.amount) for q in pending if q.total_price)
        facturable = has_content and not already_invoiced and not advance_block

        block_reason = None
        if not has_content:
            block_reason = "Sin presupuestos con contenido"
        elif already_invoiced:
            block_reason = "Completamente facturado"
        elif advance_block:
            block_reason = advance_block

        return {
            "id": pf.id,
            "reference": pf.reference,
            "name": pf.name,
            "client_name": pf.client.name if pf.client else None,
            "client_has_email": bool(pf.client and (pf.client.email or "").strip()),
            "status": pf.status,
            "facturable": facturable,
            "block_reason": block_reason,
            "pending_quote_count": len(pending),
            "accepted_remaining": accepted_remaining,
            "advances_open_net": advances_open_net,
            "pending_value": pending_value,
        }


class ProjectFileInvoiceCreateSerializer(serializers.Serializer):
    # "advance": an advance invoice of ``percentage`` of the expediente's
    # taxable base, or of ``custom_amount`` (its base) when that is given.
    invoice_mode = serializers.ChoiceField(
        choices=["percentage", "custom", "advance"], default="percentage"
    )
    percentage = serializers.DecimalField(max_digits=8, decimal_places=2, default=Decimal("100"))
    custom_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    show_vehicle_name = serializers.BooleanField(default=True)
    payment_method = serializers.CharField(required=False, allow_blank=True, default="")
    due_date = serializers.DateField(required=False, allow_null=True)
    # Emit the final invoice in the same request, skipping the proforma step.
    # Emails are never sent unless explicitly requested, matching the legacy
    # proforma → finalize flow where sending was a separate user action.
    finalize = serializers.BooleanField(default=False)
    send_email = serializers.BooleanField(default=False)  # only used with finalize
    # Deduct the expediente's advances (default plan). false is refused
    # when the invoice must deduct some of them (every closing invoice).
    deduct_advances = serializers.BooleanField(default=True)

    def validate_payment_method(self, value):
        value = (value or "").strip()
        if value and value not in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
            raise serializers.ValidationError(
                f"Método de pago inválido. Opciones: {sorted(ALLOWED_SALES_INVOICE_PAYMENT_METHODS)}"
            )
        return value

    def validate(self, data):
        date_issued = timezone.now().date()
        due_date = data.get("due_date")
        if due_date and due_date < date_issued:
            raise serializers.ValidationError(
                "La fecha de vencimiento no puede ser anterior a la fecha de emisión."
            )

        if data.get("percentage", Decimal("100")) > Decimal("100"):
            raise serializers.ValidationError(
                {"percentage": "percentage no puede superar el 100%."}
            )

        if data.get("invoice_mode") == "custom":
            custom_amount = data.get("custom_amount")
            if custom_amount is None:
                raise serializers.ValidationError(
                    {"custom_amount": "custom_amount requerido cuando invoice_mode es 'custom'."}
                )
            if custom_amount <= 0:
                raise serializers.ValidationError(
                    {"custom_amount": "custom_amount debe ser un número positivo."}
                )
        elif data.get("invoice_mode") == "advance":
            # No silent 100 % advance from the percentage default
            custom_amount = data.get("custom_amount")
            if custom_amount is None and "percentage" not in self.initial_data:
                raise serializers.ValidationError(
                    {"percentage": "percentage o custom_amount requerido cuando invoice_mode es 'advance'."}
                )
            if custom_amount is not None and custom_amount <= 0:
                raise serializers.ValidationError(
                    {"custom_amount": "custom_amount debe ser un número positivo."}
                )
        return data

    def create(self, validated_data):
        project_file = self.context["project_file"]
        company = self.context["company"]
        invoice_mode = validated_data.get("invoice_mode", "percentage")
        percentage = validated_data.get("percentage", Decimal("100"))
        custom_amount = validated_data.get("custom_amount")
        notes = validated_data.get("notes", "")
        show_vehicle_name = validated_data.get("show_vehicle_name", True)
        payment_method = validated_data.get("payment_method", "")
        due_date = validated_data.get("due_date")
        date_issued = timezone.now().date()

        if invoice_mode == "advance":
            return self._create_advance(project_file, validated_data, date_issued)

        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
        if summary["total_invoiceable"] <= 0:
            raise serializers.ValidationError("Este expediente no tiene presupuestos aceptados.")
        if not summary["can_invoice"]:
            raise serializers.ValidationError("Este expediente ya está completamente facturado.")
        if (
            invoice_mode == "custom"
            and custom_amount > summary["remaining_subtotal"] + Decimal("0.01")
        ):
            raise serializers.ValidationError(
                f"El importe no puede superar el pendiente "
                f"({summary['remaining_subtotal']:.2f} € subtotal)."
            )

        if not show_vehicle_name:
            Quote.objects.filter(
                project_file=project_file,
                status="accepted",
            ).exclude(project_name=".").update(project_name=".")

        items_data = build_items_from_summary(
            summary, invoice_mode, percentage, custom_amount,
            show_vehicle_name=show_vehicle_name,
        )
        if not items_data:
            raise serializers.ValidationError(
                "No se pudieron generar líneas de factura. Comprueba que los presupuestos tienen servicios."
            )

        try:
            with transaction.atomic():
                invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                    project_file=project_file,
                    company=company,
                    client=project_file.client,
                    date_issued=date_issued,
                    due_date=due_date,
                    items_data=items_data,
                    notes=notes,
                    payment_method=payment_method or None,
                )
                AdvanceDeductionService.apply(
                    invoice, None if validated_data.get("deduct_advances", True) else {},
                )
                invoice = self._finalize_if_requested(invoice, validated_data)
            return invoice
        except DjangoValidationError as exc:
            msg = exc.message if hasattr(exc, "message") else str(exc)
            raise serializers.ValidationError(msg) from exc

    def _create_advance(self, project_file, validated_data, date_issued):
        custom_amount = validated_data.get("custom_amount")
        try:
            with transaction.atomic():
                invoice = AdvanceInvoiceService.create(
                    project_file=project_file,
                    date_issued=date_issued,
                    due_date=validated_data.get("due_date"),
                    percentage=validated_data.get("percentage") if custom_amount is None else None,
                    amount=custom_amount,
                    notes=validated_data.get("notes", ""),
                    payment_method=validated_data.get("payment_method", "") or None,
                )
                invoice = self._finalize_if_requested(invoice, validated_data)
            return invoice
        except DjangoValidationError as exc:
            raise serializers.ValidationError(" ".join(exc.messages)) from exc

    def _finalize_if_requested(self, invoice, validated_data):
        if not validated_data.get("finalize"):
            return invoice
        user = getattr(self.context.get("request"), "user", None)
        return SalesInvoiceService.finalize_invoice(
            invoice,
            send_email=validated_data.get("send_email", False),
            reply_to=sender_reply_to(user),
            cc_emails=sender_cc(user),
        )


class SalesInvoiceFinalizeSerializer(serializers.Serializer):
    send_email = serializers.BooleanField(default=True)

    def save(self, **kwargs):
        invoice = self.context["invoice"]
        if invoice.status == SalesInvoiceStatus.FINAL:
            raise serializers.ValidationError("La factura ya está finalizada.")
        send_email = self.validated_data.get("send_email", True)
        try:
            return SalesInvoiceService.finalize_invoice(
                invoice,
                send_email=send_email,
                reply_to=self._sender_reply_to(),
                cc_emails=self._sender_cc(),
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc

    def _sender_reply_to(self):
        return sender_reply_to(self._request_user())

    def _sender_cc(self):
        return sender_cc(self._request_user())

    def _request_user(self):
        request = self.context.get("request")
        return getattr(request, "user", None)


class SalesInvoiceSendEmailSerializer(serializers.Serializer):
    email = serializers.CharField(required=False, allow_blank=True, default="")

    def save(self, **kwargs):
        invoice = self.context["invoice"]
        if invoice.status != SalesInvoiceStatus.FINAL:
            raise serializers.ValidationError(
                "Solo se puede reenviar el correo de facturas finalizadas."
            )
        raw_email = self.validated_data.get("email", "").strip()
        request = self.context.get("request")
        email_meta = SalesInvoiceService.queue_client_email(
            invoice,
            to_email=raw_email or None,
            reply_to=sender_reply_to(getattr(request, "user", None)),
        )
        if email_meta.get("email_skip_reason") == "no_client_email":
            self.email_meta = email_meta
            raise serializers.ValidationError("El cliente no tiene email registrado.")
        self.email_meta = email_meta
        return invoice


class SalesInvoicePaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField(required=False, allow_blank=True)
    payment_date = serializers.DateField()
    method = serializers.ChoiceField(
        choices=[m.value for m in PaymentMethod],
        default=PaymentMethod.TRANSFER.value,
    )
    reference = serializers.CharField(required=False, allow_null=True, allow_blank=True)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("amount debe ser un número positivo.")
        return value

    def create(self, validated_data):
        invoice = self.context["invoice"]
        company = self.context["company"]
        amount = validated_data["amount"]
        currency = validated_data.get("currency") or invoice.total_amount_currency
        payment_date = validated_data["payment_date"]
        method = validated_data["method"]
        reference = validated_data.get("reference") or None

        remaining = invoice.amount_remaining.amount
        if amount > remaining + Decimal("0.01"):
            raise serializers.ValidationError(
                f"El importe ({amount}) supera el pendiente ({remaining:.2f})."
            )

        with transaction.atomic():
            payment = Payment.objects.create(
                company=company,
                payment_date=payment_date,
                amount=Money(amount, currency),
                direction=PaymentDirection.INBOUND,
                method=method,
                status=PaymentStatus.CONFIRMED,
                reference=reference,
                amount_pending_allocation=Money(amount, currency),
            )
            PaymentAllocation.objects.create(
                payment=payment,
                content_type=ContentType.objects.get_for_model(invoice),
                object_id=invoice.pk,
                amount_applied=Money(amount, currency),
            )

        invoice.refresh_from_db()
        return invoice


class BulkEmitInvoicesSerializer(serializers.Serializer):
    project_file_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    payment_method = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    show_vehicle_name = serializers.BooleanField(default=True)
    send_email = serializers.BooleanField(default=True)
    due_date = serializers.DateField(required=False, allow_null=True)
    # None → fall back to Company.bulk_emit_copy_quote_observations
    include_quote_observations = serializers.BooleanField(
        required=False, allow_null=True, default=None
    )

    def validate_payment_method(self, value):
        value = (value or "").strip()
        if value and value not in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
            raise serializers.ValidationError(
                f"Método de pago inválido. Opciones: {sorted(ALLOWED_SALES_INVOICE_PAYMENT_METHODS)}"
            )
        return value

    def validate(self, data):
        due_date = data.get("due_date")
        if due_date and due_date < timezone.now().date():
            raise serializers.ValidationError(
                "La fecha de vencimiento no puede ser anterior a la fecha de emisión."
            )
        return data

    def save(self, **kwargs):
        company = self.context["company"]
        project_file_ids = self.validated_data["project_file_ids"]
        payment_method = self.validated_data.get("payment_method", "")
        notes = self.validated_data.get("notes", "")
        show_vehicle_name = self.validated_data.get("show_vehicle_name", True)
        send_email = self.validated_data.get("send_email", True)
        due_date = self.validated_data.get("due_date")
        include_observations = self.validated_data.get("include_quote_observations")
        if include_observations is None:
            include_observations = company.bulk_emit_copy_quote_observations
        date_issued = timezone.now().date()

        pf_map = {
            pf.id: pf
            for pf in ProjectFile.objects.filter(
                id__in=project_file_ids, company=company
            ).select_related("client")
        }

        successes = []
        failures = []

        for pf_id in project_file_ids:
            pf = pf_map.get(pf_id)
            if pf is None:
                failures.append({"project_file_id": pf_id, "reason": "Expediente no encontrado."})
                continue

            try:
                with transaction.atomic():
                    pending_quotes = list(
                        Quote.objects.prefetch_related(
                            "configurations", "items", "inventory_items"
                        ).filter(
                            project_file=pf,
                            status__in=[QuoteStatus.DRAFT, QuoteStatus.SENT],
                        )
                    )
                    for quote in pending_quotes:
                        if quote_has_content(quote):
                            quote.status = QuoteStatus.ACCEPTED
                            quote.save()

                    summary = ProjectFileInvoiceService.get_invoiceable_summary(pf)
                    if summary["total_invoiceable"] <= 0:
                        failures.append({
                            "project_file_id": pf_id,
                            "reason": "Sin presupuestos con contenido.",
                        })
                        continue
                    if not summary["can_invoice"]:
                        failures.append({
                            "project_file_id": pf_id,
                            "reason": "Ya está completamente facturado.",
                        })
                        continue

                    items_data = build_items_from_summary(
                        summary, "percentage", Decimal("100"), None,
                        show_vehicle_name=show_vehicle_name,
                    )
                    if not items_data:
                        failures.append({
                            "project_file_id": pf_id,
                            "reason": "No se pudieron generar líneas de factura.",
                        })
                        continue

                    invoice_notes = notes
                    if include_observations:
                        # After auto-accept, so freshly accepted quotes count too.
                        observations = (
                            ProjectFileInvoiceService.get_accepted_quote_observations(pf)
                        )
                        parts = ([notes.strip()] if notes.strip() else []) + observations
                        invoice_notes = "\n---\n".join(parts)

                    invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                        project_file=pf,
                        company=company,
                        client=pf.client,
                        date_issued=date_issued,
                        due_date=due_date,
                        items_data=items_data,
                        notes=invoice_notes,
                        payment_method=payment_method or None,
                    )
                    # 100 % of what is left: it takes every advance pending
                    AdvanceDeductionService.apply(invoice)
                    invoice = SalesInvoiceService.finalize_invoice(invoice, send_email=send_email)

                auto_accepted = sum(1 for q in pending_quotes if quote_has_content(q))
                email_meta = SalesInvoiceService.get_finalize_email_status(invoice)
                successes.append({
                    "project_file_id": pf_id,
                    "invoice_id": invoice.id,
                    "invoice_number": invoice.invoice_number,
                    "total_amount": float(invoice.total_amount.amount) if invoice.total_amount else 0.0,
                    "client_name": pf.client.name if pf.client else None,
                    "project_file_reference": pf.reference,
                    "project_file_name": pf.name,
                    "auto_accepted_quotes": auto_accepted,
                    **email_meta,
                })

            except Exception as exc:
                msg = exc.message if hasattr(exc, "message") else str(exc)
                failures.append({"project_file_id": pf_id, "reason": msg})

        status_code = 200 if successes else 400
        return {
            "successes": successes,
            "failures": failures,
            "total_created": len(successes),
            "total_failed": len(failures),
            "_status_code": status_code,
        }
