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
from invoices.models import SalesInvoice, SalesInvoiceStatus
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


def get_config_component_lines(config) -> list:
    override_subtotal = Decimal(str(config.subtotal_price.amount))

    pb = config.pricing_breakdown or {}
    lines = pb.get("lines", [])
    is_override_only = (
        len(lines) == 1 and lines[0].get("type") == "override"
    ) or (not lines and config.price_override and config.price_override.amount > 0)

    if not is_override_only and lines:
        return [
            {"label": line.get("label", ""), "amount": Decimal(str(line.get("amount", 0)))}
            for line in lines
            if Decimal(str(line.get("amount", 0))) != 0
        ]

    bom_lines = []
    bom_total = Decimal("0")
    for item in config.items.all():
        lt = Decimal(str(item.line_total.amount)) if item.line_total else Decimal("0")
        if lt <= 0:
            continue
        label = item.label or (item.component.name if item.component else "")
        if " × " in label:
            label = label.rsplit(" × ", 1)[0].strip()
        bom_lines.append({"label": label, "amount": lt})
        bom_total += lt

    if bom_lines:
        scale = override_subtotal / bom_total if bom_total else Decimal("1")
        return [
            {"label": bl["label"], "amount": (bl["amount"] * scale).quantize(Decimal("0.01"))}
            for bl in bom_lines
        ]

    return [{"label": config.name, "amount": override_subtotal}]


def build_items_from_summary(summary, invoice_mode, percentage, custom_amount, show_vehicle_name=True):
    ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
        summary, invoice_mode, percentage, custom_amount
    )
    items = []

    for quote_data in summary["quotes"]:
        quote = quote_data["quote"]
        quote_name = quote.project_name if show_vehicle_name else "."
        for config in quote.configurations.prefetch_related("items__component").all():
            sub = Decimal(str(config.subtotal_price.amount))
            if sub <= 0:
                continue
            for comp in get_config_component_lines(config):
                comp_price = (comp["amount"] * ratio).quantize(Decimal("0.01"))
                if comp_price <= 0:
                    continue
                items.append({
                    "source_id": f"config_{config.id}",
                    "description": comp["label"],
                    "quote_name": quote_name,
                    "quantity": Decimal("1"),
                    "unit_price": comp_price,
                    "discount_percentage": Decimal("0.00"),
                })

        for item in quote.items.all():
            item_price = (item.line_total.amount * ratio).quantize(Decimal("0.01"))
            if item_price <= 0:
                continue
            items.append({
                "source_id": f"quote_item_{item.id}",
                "description": f"{item.material} x{item.quantity} (P#{quote.id})",
                "quote_name": quote_name,
                "quantity": Decimal("1"),
                "unit_price": item_price,
                "discount_percentage": Decimal("0.00"),
            })

        for inv_item in quote.inventory_items.select_related("inventory_item").all():
            line_amt = inv_item.line_total.amount if inv_item.line_total else Decimal("0.00")
            if line_amt <= 0:
                continue
            inv_name = (
                inv_item.inventory_item.name
                if inv_item.inventory_item_id
                else f"Inventory Item #{inv_item.pk}"
            )
            items.append({
                "source_id": f"inv_item_{inv_item.id}",
                "description": inv_name,
                "quote_name": quote_name,
                "quantity": inv_item.quantity,
                "unit_price": inv_item.unit_price.amount,
                "discount_percentage": inv_item.discount,
            })

    for config_data in summary.get("direct_configurations", []):
        sub = config_data["subtotal"]
        item_price = (sub * ratio).quantize(Decimal("0.01"))
        if item_price <= 0:
            continue
        items.append({
            "source_id": f"direct_{config_data['id']}",
            "description": config_data["name"],
            "quantity": Decimal("1"),
            "unit_price": item_price,
            "discount_percentage": Decimal("0.00"),
        })

    return items


class SalesInvoiceDetailSerializer(serializers.Serializer):
    """Read serializer for a SalesInvoice with items, payments and email log."""

    def to_representation(self, invoice):
        ct = ContentType.objects.get_for_model(invoice)
        is_final = invoice.status == SalesInvoiceStatus.FINAL
        if not is_final:
            email_log_status = None
            email_sent_at = None
            email_error = None
        elif hasattr(invoice, "_email_log_status"):
            email_log_status = invoice._email_log_status
            email_sent_at = (
                invoice._email_log_sent_at.isoformat()
                if invoice._email_log_sent_at
                else None
            )
            email_error = invoice._email_log_error
        else:
            email_log = (
                EmailLog.objects.filter(content_type=ct, object_id=invoice.pk)
                .order_by("-sent_at")
                .first()
            )
            email_log_status = email_log.status if email_log else None
            email_sent_at = email_log.sent_at.isoformat() if email_log else None
            email_error = email_log.error_message if email_log else None

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
            }
            for item in invoice.items.all()
        ]
        return {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "status": invoice.status,
            "payment_status": invoice.payment_status,
            "payment_method": invoice.payment_method,
            "payment_method_label": invoice.get_payment_method_display(),
            "date_issued": invoice.date_issued.strftime("%Y-%m-%d") if invoice.date_issued else None,
            "due_date": invoice.due_date.strftime("%Y-%m-%d") if invoice.due_date else None,
            "subtotal_amount": float(invoice.subtotal_amount.amount) if invoice.subtotal_amount else 0.0,
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
        }


class SalesInvoiceListItemSerializer(serializers.Serializer):
    def to_representation(self, invoice):
        return {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "status": invoice.status,
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
        accepted_total = sum(float(q.total_price.amount) for q in accepted if q.total_price)
        total_invoiced = float(
            ProjectFileInvoiceService.get_invoiceable_summary(pf)["total_invoiced"]
        ) if accepted else 0.0
        accepted_remaining = max(0.0, accepted_total - total_invoiced)
        already_invoiced = has_content and accepted and accepted_remaining < 0.01 and not pending
        pending_value = sum(float(q.total_price.amount) for q in pending if q.total_price)
        facturable = has_content and not already_invoiced

        block_reason = None
        if not has_content:
            block_reason = "Sin presupuestos con contenido"
        elif already_invoiced:
            block_reason = "Completamente facturado"

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
            "pending_value": pending_value,
        }


class ProjectFileInvoiceCreateSerializer(serializers.Serializer):
    invoice_mode = serializers.ChoiceField(choices=["percentage", "custom"], default="percentage")
    percentage = serializers.DecimalField(max_digits=8, decimal_places=2, default=Decimal("100"))
    custom_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    show_vehicle_name = serializers.BooleanField(default=True)
    payment_method = serializers.CharField(required=False, allow_blank=True, default="")
    due_date = serializers.DateField(required=False, allow_null=True)

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

        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
        if summary["total_invoiceable"] <= 0:
            raise serializers.ValidationError("Este expediente no tiene presupuestos aceptados.")
        if summary["remaining"] <= Decimal("0.01"):
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
            return ProjectFileInvoiceService.create_invoice_from_project_file(
                project_file=project_file,
                company=company,
                client=project_file.client,
                date_issued=date_issued,
                due_date=due_date,
                items_data=items_data,
                notes=notes,
                payment_method=payment_method or None,
            )
        except DjangoValidationError as exc:
            msg = exc.message if hasattr(exc, "message") else str(exc)
            raise serializers.ValidationError(msg) from exc


class SalesInvoiceFinalizeSerializer(serializers.Serializer):
    send_email = serializers.BooleanField(default=True)

    def save(self, **kwargs):
        invoice = self.context["invoice"]
        if invoice.status == SalesInvoiceStatus.FINAL:
            raise serializers.ValidationError("La factura ya está finalizada.")
        send_email = self.validated_data.get("send_email", True)
        return SalesInvoiceService.finalize_invoice(invoice, send_email=send_email)


class SalesInvoiceSendEmailSerializer(serializers.Serializer):
    email = serializers.CharField(required=False, allow_blank=True, default="")

    def save(self, **kwargs):
        invoice = self.context["invoice"]
        if invoice.status != SalesInvoiceStatus.FINAL:
            raise serializers.ValidationError(
                "Solo se puede reenviar el correo de facturas finalizadas."
            )
        raw_email = self.validated_data.get("email", "").strip()
        email_meta = SalesInvoiceService.queue_client_email(
            invoice,
            to_email=raw_email or None,
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
            payment, created = Payment.objects.get_or_create(
                company=company,
                payment_date=payment_date,
                amount=Money(amount, currency),
                direction=PaymentDirection.INBOUND,
                method=method,
                reference=reference,
                defaults={
                    "status": PaymentStatus.CONFIRMED,
                    "amount_pending_allocation": Money(amount, currency),
                },
            )
            if created:
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
                    if summary["remaining"] <= Decimal("0.01"):
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

                    invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                        project_file=pf,
                        company=company,
                        client=pf.client,
                        date_issued=date_issued,
                        due_date=due_date,
                        items_data=items_data,
                        notes=notes,
                        payment_method=payment_method or None,
                    )
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
