"""API views for creating and managing SalesInvoices from ProjectFiles."""

import json
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from api._utils import CompanyAPIView, parse_date_or_error
from configurator.models import ExportTemplateKind
from core.services.pdf_service import PDFService
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
from djmoney.money import Money



def _serialize_invoice_detail(invoice):
    ct = ContentType.objects.get_for_model(invoice)
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
        "client_name": invoice.client.name if invoice.client else None,
        "client_email": (invoice.client.email or "").strip() or None if invoice.client else None,
        "project_file_id": invoice.project_file_id,
        "items": items,
        "payments": payments,
    }


def _get_config_component_lines(config) -> list:
    """Return per-component label/amount lines for a configuration.

    Priority: stored pricing_breakdown non-override lines → BOM items → single fallback.
    Amounts sum to config.subtotal_price (before any invoicing ratio is applied).
    """
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
    for item in config.items.all():  # prefetched via items__component
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


def _build_items_from_summary(summary, invoice_mode, percentage, custom_amount, show_vehicle_name=True):
    """Build invoice line items scaled to the pending amount (partial invoicing).

    Each configuration is expanded into one item per component so the stored
    invoice items already carry the breakdown — no reverse lookup needed at
    export time.  All component items for the same config share source_id
    "config_<id>" so the export strategy can group them back by config.

    Inventory items are billed at full price (not scaled by ratio) — they are
    either on the invoice or not.

    When show_vehicle_name is True the quote's project_name is appended to each
    component description so the PDF template can show it without a DB lookup.
    """
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

            for comp in _get_config_component_lines(config):
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

        # Inventory items — billed at full price, not scaled by ratio
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


class ProjectFileInvoiceCreateView(CompanyAPIView):
    """POST project-files/<id>/invoices/ — create a SalesInvoice from a ProjectFile."""

    def post(self, request, project_file_id):
        try:
            project_file = ProjectFile.objects.get(id=project_file_id, company=self.company)
        except ProjectFile.DoesNotExist:
            return JsonResponse({"error": "Expediente no encontrado."}, status=404)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "JSON inválido."}, status=400)

        invoice_mode = body.get("invoice_mode", "percentage")
        if invoice_mode not in ("percentage", "custom"):
            return JsonResponse({"error": "invoice_mode debe ser 'percentage' o 'custom'."}, status=400)

        try:
            percentage = Decimal(str(body.get("percentage", 100)))
        except Exception:
            return JsonResponse({"error": "percentage inválido."}, status=400)

        custom_amount = body.get("custom_amount")
        if invoice_mode == "custom":
            if custom_amount is None:
                return JsonResponse({"error": "custom_amount requerido cuando invoice_mode es 'custom'."}, status=400)
            try:
                custom_amount = Decimal(str(custom_amount))
                if custom_amount <= 0:
                    raise ValueError
            except Exception:
                return JsonResponse({"error": "custom_amount debe ser un número positivo."}, status=400)

        notes = body.get("notes", "")
        show_vehicle_name = bool(body.get("show_vehicle_name", True))
        payment_method = (body.get("payment_method") or "").strip()
        if payment_method and payment_method not in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
            return JsonResponse(
                {
                    "error": (
                        f"Método de pago inválido. Opciones: "
                        f"{sorted(ALLOWED_SALES_INVOICE_PAYMENT_METHODS)}"
                    ),
                },
                status=400,
            )

        date_issued = timezone.now().date()

        due_date = None
        if body.get("due_date"):
            due_date, err = parse_date_or_error(body.get("due_date"))
            if err:
                return err
            if due_date < date_issued:
                return JsonResponse(
                    {"error": "La fecha de vencimiento no puede ser anterior a la fecha de emisión."},
                    status=400,
                )

        try:
            summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)

            if summary["total_invoiceable"] <= 0:
                return JsonResponse(
                    {"error": "Este expediente no tiene presupuestos aceptados."},
                    status=400,
                )
            if summary["remaining"] <= Decimal("0.01"):
                return JsonResponse(
                    {"error": "Este expediente ya está completamente facturado."},
                    status=400,
                )

            if invoice_mode == "custom" and custom_amount > summary["remaining_subtotal"] + Decimal("0.01"):
                return JsonResponse(
                    {
                        "error": (
                            f"El importe no puede superar el pendiente "
                            f"({summary['remaining_subtotal']:.2f} € subtotal)."
                        ),
                    },
                    status=400,
                )

            if not show_vehicle_name:
                from sales.models import Quote
                Quote.objects.filter(
                    project_file=project_file,
                    status="accepted",
                ).exclude(project_name=".").update(project_name=".")

            items_data = _build_items_from_summary(
                summary, invoice_mode, percentage, custom_amount,
                show_vehicle_name=show_vehicle_name,
            )
            if not items_data:
                return JsonResponse(
                    {"error": "No se pudieron generar líneas de factura. Comprueba que los presupuestos tienen servicios."},
                    status=400,
                )

            invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                project_file=project_file,
                company=self.company,
                client=project_file.client,
                date_issued=date_issued,
                due_date=due_date,
                items_data=items_data,
                notes=notes,
                payment_method=payment_method or None,
            )
        except ValidationError as e:
            msg = e.message if hasattr(e, "message") else str(e)
            return JsonResponse({"error": msg}, status=400)

        return JsonResponse(_serialize_invoice_detail(invoice), status=201)


class SalesInvoiceDetailView(CompanyAPIView):
    """GET sales-invoices/<id>/ — detail with payments."""

    def get(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.select_related("client").get(
                id=invoice_id, company=self.company
            )
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        return JsonResponse(_serialize_invoice_detail(invoice))


class SalesInvoicePdfView(CompanyAPIView):
    """GET sales-invoices/<id>/pdf/ — same export template as admin View PDF (WeasyPrint)."""

    def perform_content_negotiation(self, request, force=False):
        # DRF only registers JSON renderers; Accept: application/pdf would 406 otherwise.
        from rest_framework.renderers import JSONRenderer

        return (JSONRenderer(), "application/json")

    def get(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.select_related("client", "company").get(
                id=invoice_id, company=self.company
            )
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        show_vehicle = request.GET.get('show_vehicle', 'true').lower() != 'false'
        pdf_bytes = PDFService.render_object_pdf(
            invoice,
            ExportTemplateKind.SALES_INVOICE,
            company=invoice.company,
            extra_context={'show_vehicle': show_vehicle},
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

        if invoice.status != SalesInvoiceStatus.FINAL:
            return JsonResponse(
                {"error": "Solo se puede reenviar el correo de facturas finalizadas."},
                status=400,
            )

        try:
            body = json.loads(request.body or "{}")
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "JSON inválido."}, status=400)

        raw_email = (body.get("email") or "").strip()
        email_meta = SalesInvoiceService.queue_client_email(
            invoice,
            to_email=raw_email or None,
        )
        if email_meta.get("email_skip_reason") == "no_client_email":
            return JsonResponse(
                {
                    "error": "El cliente no tiene email registrado.",
                    **email_meta,
                },
                status=400,
            )

        payload = _serialize_invoice_detail(invoice)
        payload.update(email_meta)
        return JsonResponse(payload)


class SalesInvoiceFinalizeView(CompanyAPIView):
    """POST sales-invoices/<id>/finalize/ — convert PROFORMA → FINAL."""

    def post(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.get(id=invoice_id, company=self.company)
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        if invoice.status == SalesInvoiceStatus.FINAL:
            return JsonResponse({"error": "La factura ya está finalizada."}, status=400)

        try:
            body = json.loads(request.body or "{}")
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "JSON inválido."}, status=400)

        send_email = body.get("send_email", True)
        if not isinstance(send_email, bool):
            return JsonResponse({"error": "send_email debe ser booleano."}, status=400)

        invoice = SalesInvoiceService.finalize_invoice(invoice, send_email=send_email)
        payload = _serialize_invoice_detail(invoice)
        payload.update(SalesInvoiceService.get_finalize_email_status(invoice))
        return JsonResponse(payload)


class SalesInvoicePaymentView(CompanyAPIView):
    """POST sales-invoices/<id>/payments/ — register an inbound payment."""

    def post(self, request, invoice_id):
        try:
            invoice = SalesInvoice.objects.get(id=invoice_id, company=self.company)
        except SalesInvoice.DoesNotExist:
            return JsonResponse({"error": "Factura no encontrada."}, status=404)

        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "JSON inválido."}, status=400)

        try:
            amount = Decimal(str(body["amount"]))
            if amount <= 0:
                raise ValueError
        except (KeyError, Exception):
            return JsonResponse({"error": "amount debe ser un número positivo."}, status=400)

        currency = body.get("currency", invoice.total_amount_currency)

        payment_date_raw = body.get("payment_date")
        if not payment_date_raw:
            return JsonResponse({"error": "payment_date es requerido (YYYY-MM-DD)."}, status=400)
        payment_date, err = parse_date_or_error(payment_date_raw)
        if err:
            return err

        method = body.get("method", "transfer")
        if method not in [m.value for m in PaymentMethod]:
            return JsonResponse(
                {"error": f"Método de pago inválido. Opciones: {[m.value for m in PaymentMethod]}"},
                status=400,
            )

        reference = body.get("reference") or None

        remaining = invoice.amount_remaining.amount
        if amount > remaining + Decimal("0.01"):
            return JsonResponse(
                {"error": f"El importe ({amount}) supera el pendiente ({remaining:.2f})."},
                status=400,
            )

        with transaction.atomic():
            payment, created = Payment.objects.get_or_create(
                company=self.company,
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
        return JsonResponse(_serialize_invoice_detail(invoice), status=201)


class BulkEmitCandidatesView(CompanyAPIView):
    """GET sales-invoices/bulk-emit-candidates/ — open expedientes with their invoicing eligibility."""

    def get(self, request):
        from django.core.paginator import Paginator
        from django.db.models import Q
        from sales.models import Quote, QuoteStatus

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

        results = []
        for pf in page:
            all_quotes = list(pf.quotes.all())

            # Accepted quotes with content
            accepted = [q for q in all_quotes if q.status == QuoteStatus.ACCEPTED and _quote_has_content(q)]
            # Pending quotes (draft/sent) with content — candidates for auto-accept
            pending = [q for q in all_quotes if q.status in (QuoteStatus.DRAFT, QuoteStatus.SENT) and _quote_has_content(q)]

            has_content = bool(accepted or pending)

            # Compute remaining from accepted quotes
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

            results.append({
                "id": pf.id,
                "reference": pf.reference,
                "name": pf.name,
                "client_name": pf.client.name if pf.client else None,
                "status": pf.status,
                "facturable": facturable,
                "block_reason": block_reason,
                "pending_quote_count": len(pending),
                "accepted_remaining": accepted_remaining,
                "pending_value": pending_value,
            })

        return JsonResponse({
            "results": results,
            "count": paginator.count,
            "has_next": page.has_next(),
            "page": page_num,
        })


def _quote_has_content(quote):
    """True if the quote has at least one configuration or item with non-zero value."""
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


class BulkEmitInvoicesView(CompanyAPIView):
    """POST sales-invoices/bulk-emit/ — create and finalize one invoice per ProjectFile at 100%."""

    def post(self, request):
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({"error": "JSON inválido."}, status=400)

        project_file_ids = body.get("project_file_ids", [])
        if not isinstance(project_file_ids, list) or not project_file_ids:
            return JsonResponse({"error": "project_file_ids debe ser una lista no vacía."}, status=400)

        payment_method = (body.get("payment_method") or "").strip()
        if payment_method and payment_method not in ALLOWED_SALES_INVOICE_PAYMENT_METHODS:
            return JsonResponse(
                {"error": f"Método de pago inválido. Opciones: {sorted(ALLOWED_SALES_INVOICE_PAYMENT_METHODS)}"},
                status=400,
            )

        notes = body.get("notes", "")
        show_vehicle_name = bool(body.get("show_vehicle_name", True))

        date_issued = timezone.now().date()

        due_date = None
        if body.get("due_date"):
            due_date, err = parse_date_or_error(body.get("due_date"))
            if err:
                return err
            if due_date < date_issued:
                return JsonResponse(
                    {"error": "La fecha de vencimiento no puede ser anterior a la fecha de emisión."},
                    status=400,
                )

        pf_map = {
            pf.id: pf
            for pf in ProjectFile.objects.filter(
                id__in=project_file_ids, company=self.company
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
                    # Auto-accept pending quotes that have content
                    from sales.models import Quote, QuoteStatus
                    pending_quotes = list(
                        Quote.objects.prefetch_related(
                            "configurations", "items", "inventory_items"
                        ).filter(
                            project_file=pf,
                            status__in=[QuoteStatus.DRAFT, QuoteStatus.SENT],
                        )
                    )
                    for quote in pending_quotes:
                        if _quote_has_content(quote):
                            quote.status = QuoteStatus.ACCEPTED
                            quote.save()

                    summary = ProjectFileInvoiceService.get_invoiceable_summary(pf)

                    if summary["total_invoiceable"] <= 0:
                        failures.append({"project_file_id": pf_id, "reason": "Sin presupuestos con contenido."})
                        continue

                    if summary["remaining"] <= Decimal("0.01"):
                        failures.append({"project_file_id": pf_id, "reason": "Ya está completamente facturado."})
                        continue

                    items_data = _build_items_from_summary(
                        summary, "percentage", Decimal("100"), None,
                        show_vehicle_name=show_vehicle_name,
                    )

                    if not items_data:
                        failures.append({"project_file_id": pf_id, "reason": "No se pudieron generar líneas de factura."})
                        continue

                    invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                        project_file=pf,
                        company=self.company,
                        client=pf.client,
                        date_issued=date_issued,
                        due_date=due_date,
                        items_data=items_data,
                        notes=notes,
                        payment_method=payment_method or None,
                    )
                    invoice = SalesInvoiceService.finalize_invoice(invoice, send_email=False)

                auto_accepted = sum(1 for q in pending_quotes if _quote_has_content(q))
                successes.append({
                    "project_file_id": pf_id,
                    "invoice_id": invoice.id,
                    "invoice_number": invoice.invoice_number,
                    "total_amount": float(invoice.total_amount.amount) if invoice.total_amount else 0.0,
                    "client_name": pf.client.name if pf.client else None,
                    "project_file_reference": pf.reference,
                    "project_file_name": pf.name,
                    "auto_accepted_quotes": auto_accepted,
                })

            except Exception as e:
                msg = e.message if hasattr(e, "message") else str(e)
                failures.append({"project_file_id": pf_id, "reason": msg})

        status_code = 200 if successes else 400
        return JsonResponse({
            "successes": successes,
            "failures": failures,
            "total_created": len(successes),
            "total_failed": len(failures),
        }, status=status_code)


class SalesInvoiceListView(CompanyAPIView):
    """GET sales-invoices/ — paginated list of all SalesInvoices for the company."""

    def get(self, request):
        from django.core.paginator import Paginator

        qs = (
            SalesInvoice.objects.filter(company=self.company)
            .select_related("client")
            .order_by("-date_issued", "-id")
        )

        payment_status = request.GET.get("payment_status")
        if payment_status:
            qs = qs.filter(payment_status=payment_status)

        status_filter = request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        search = request.GET.get("search", "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(invoice_number__icontains=search) | Q(client__name__icontains=search)
            )

        try:
            page_num = max(1, int(request.GET.get("page", 1)))
        except ValueError:
            page_num = 1

        paginator = Paginator(qs, 20)
        page = paginator.get_page(page_num)

        results = [
            {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "status": inv.status,
                "payment_status": inv.payment_status,
                "date_issued": inv.date_issued.strftime("%Y-%m-%d") if inv.date_issued else None,
                "total_amount": float(inv.total_amount.amount) if inv.total_amount else 0.0,
                "amount_remaining": float(inv.amount_remaining.amount) if inv.amount_remaining else 0.0,
                "client_name": inv.client.name if inv.client else None,
                "project_file_id": inv.project_file_id,
            }
            for inv in page
        ]

        return JsonResponse({
            "results": results,
            "count": paginator.count,
            "page": page_num,
            "has_next": page.has_next(),
        })
