"""
Invoice tools — Purchase (received) and Sales invoices.
"""

from __future__ import annotations

from chatbot.interfaces import BaseTool, ChatContext, ToolExecutionError
from chatbot.registry import register_tool


@register_tool
class ListPurchaseInvoices(BaseTool):
    """List received (purchase) invoices."""

    @property
    def name(self) -> str:
        return "list_purchase_invoices"

    @property
    def description(self) -> str:
        return (
            "List received (purchase) invoices. "
            "Filter by provider, payment status (unpaid, partial, paid), "
            "or confirmation status (approved, pending, declined). "
            "Returns invoice number, provider, amounts, dates, and status."
        )

    @property
    def required_permission(self) -> str | None:
        return "invoices.view_invoice"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "provider_id": {
                    "type": "integer",
                    "description": "Filter by provider/supplier ID.",
                },
                "payment_status": {
                    "type": "string",
                    "description": "Filter by payment status.",
                    "enum": ["unpaid", "partial", "paid"],
                },
                "confirmation_status": {
                    "type": "string",
                    "description": "Filter by confirmation status.",
                    "enum": ["approved", "pending", "declined"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results.",
                    "default": 25,
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from invoices.models import Invoice

        qs = Invoice.objects.filter(company_id=context.company_id).select_related("provider")

        if provider_id := params.get("provider_id"):
            qs = qs.filter(provider_id=provider_id)
        if payment_status := params.get("payment_status"):
            qs = qs.filter(payment_status=payment_status)
        if confirmation_status := params.get("confirmation_status"):
            qs = qs.filter(confirmation_status=confirmation_status)

        limit = min(params.get("limit", 25), 100)
        invoices = qs.order_by("-date_issued")[:limit]

        return {
            "total_count": qs.count(),
            "returned_count": len(invoices),
            "invoices": [
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number or "",
                    "provider": str(inv.provider) if inv.provider else "",
                    "date_issued": inv.date_issued.strftime("%d/%m/%Y") if inv.date_issued else "",
                    "due_date": inv.due_date.strftime("%d/%m/%Y") if inv.due_date else "",
                    "net_amount": str(inv.net_amount) if inv.net_amount else "0",
                    "total_amount": str(inv.total_amount) if inv.total_amount else "0",
                    "payment_status": inv.get_payment_status_display(),
                    "confirmation_status": inv.get_confirmation_status_display(),
                    "payment_method": inv.get_payment_method_display(),
                    "amount_paid": str(inv.amount_paid),
                    "amount_remaining": str(inv.amount_remaining),
                }
                for inv in invoices
            ],
        }


@register_tool
class ListSalesInvoices(BaseTool):
    """List sales invoices."""

    @property
    def name(self) -> str:
        return "list_sales_invoices"

    @property
    def description(self) -> str:
        return (
            "List sales invoices (facturas de venta). "
            "Filter by client, status (proforma, final), "
            "or payment status (unpaid, partial, paid)."
        )

    @property
    def required_permission(self) -> str | None:
        return "invoices.view_salesinvoice"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "integer",
                    "description": "Filter by client ID.",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by invoice status.",
                    "enum": ["proforma", "final"],
                },
                "payment_status": {
                    "type": "string",
                    "description": "Filter by payment status.",
                    "enum": ["unpaid", "partial", "paid"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results.",
                    "default": 25,
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from invoices.models import SalesInvoice

        qs = SalesInvoice.objects.filter(
            company_id=context.company_id
        ).select_related("client")

        if client_id := params.get("client_id"):
            qs = qs.filter(client_id=client_id)
        if status := params.get("status"):
            qs = qs.filter(status=status)
        if payment_status := params.get("payment_status"):
            qs = qs.filter(payment_status=payment_status)

        limit = min(params.get("limit", 25), 100)
        invoices = qs.order_by("-date_issued")[:limit]

        return {
            "total_count": qs.count(),
            "returned_count": len(invoices),
            "invoices": [
                {
                    "id": inv.id,
                    "invoice_number": inv.display_number,
                    "client": str(inv.client) if inv.client else "",
                    "status": inv.get_status_display(),
                    "date_issued": inv.date_issued.strftime("%d/%m/%Y") if inv.date_issued else "",
                    "due_date": inv.due_date.strftime("%d/%m/%Y") if inv.due_date else "",
                    "subtotal": str(inv.taxable_base),
                    "discount": str(inv.discount_amount),
                    "tax": str(inv.tax_amount),
                    "total": str(inv.total_amount),
                    "payment_status": inv.get_payment_status_display(),
                    "reference": inv.reference or "",
                }
                for inv in invoices
            ],
        }


@register_tool
class GetInvoiceDetail(BaseTool):
    """Get full detail for a purchase or sales invoice."""

    @property
    def name(self) -> str:
        return "get_invoice_detail"

    @property
    def description(self) -> str:
        return (
            "Get detailed information for a specific invoice, including "
            "line items and applied taxes. Specify the invoice type: "
            "'purchase' for received invoices, 'sales' for sales invoices."
        )

    @property
    def required_permission(self) -> str | None:
        return "invoices.view_invoice"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "invoice_id": {
                    "type": "integer",
                    "description": "The ID of the invoice.",
                },
                "invoice_type": {
                    "type": "string",
                    "description": "Type of invoice: 'purchase' or 'sales'.",
                    "enum": ["purchase", "sales"],
                },
            },
            "required": ["invoice_id", "invoice_type"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        invoice_type = params["invoice_type"]
        invoice_id = params["invoice_id"]

        if invoice_type == "purchase":
            return self._get_purchase_invoice(context, invoice_id)
        elif invoice_type == "sales":
            return self._get_sales_invoice(context, invoice_id)
        else:
            raise ToolExecutionError(f"Invalid invoice_type: {invoice_type}")

    def _get_purchase_invoice(self, context: ChatContext, invoice_id: int) -> dict:
        from invoices.models import Invoice

        try:
            inv = Invoice.objects.select_related("provider", "account").get(
                pk=invoice_id, company_id=context.company_id
            )
        except Invoice.DoesNotExist:
            raise ToolExecutionError(f"Purchase invoice #{invoice_id} not found.")

        items = [
            {
                "description": item.description if hasattr(item, "description") else "",
                "quantity": str(item.quantity) if hasattr(item, "quantity") else "",
                "unit_price": str(item.unit_price) if hasattr(item, "unit_price") else "",
                "line_total": str(item.line_total) if hasattr(item, "line_total") else "",
            }
            for item in inv.items.all()
        ]

        taxes = [
            {
                "tax_name": str(tax.tax_type),
                "percentage": str(tax.selected_percentage),
                "base_amount": str(tax.base_amount) if tax.base_amount else "",
                "calculated_amount": str(tax.calculated_amount) if tax.calculated_amount else "",
            }
            for tax in inv.get_taxes()
        ]

        return {
            "type": "purchase",
            "id": inv.id,
            "invoice_number": inv.invoice_number or "",
            "provider": str(inv.provider) if inv.provider else "",
            "date_issued": inv.date_issued.strftime("%d/%m/%Y") if inv.date_issued else "",
            "due_date": inv.due_date.strftime("%d/%m/%Y") if inv.due_date else "",
            "net_amount": str(inv.net_amount) if inv.net_amount else "0",
            "total_amount": str(inv.total_amount) if inv.total_amount else "0",
            "payment_status": inv.get_payment_status_display(),
            "payment_method": inv.get_payment_method_display(),
            "account": str(inv.account) if inv.account else "",
            "observations": inv.observations or "",
            "amount_paid": str(inv.amount_paid),
            "amount_remaining": str(inv.amount_remaining),
            "is_balanced": inv.is_total_balanced,
            "items": items,
            "taxes": taxes,
        }

    def _get_sales_invoice(self, context: ChatContext, invoice_id: int) -> dict:
        from invoices.models import SalesInvoice

        try:
            inv = SalesInvoice.objects.select_related("client", "configuration").get(
                pk=invoice_id, company_id=context.company_id
            )
        except SalesInvoice.DoesNotExist:
            raise ToolExecutionError(f"Sales invoice #{invoice_id} not found.")

        items = [
            {
                "description": item.description if hasattr(item, "description") else "",
                "quantity": str(item.quantity) if hasattr(item, "quantity") else "",
                "unit_price": str(item.unit_price) if hasattr(item, "unit_price") else "",
                "line_total": str(item.line_total) if hasattr(item, "line_total") else "",
            }
            for item in inv.items.all()
        ]

        taxes = [
            {
                "tax_name": str(tax.tax_type),
                "percentage": str(tax.selected_percentage),
                "base_amount": str(tax.base_amount) if tax.base_amount else "",
                "calculated_amount": str(tax.calculated_amount) if tax.calculated_amount else "",
            }
            for tax in inv.get_taxes()
        ]

        return {
            "type": "sales",
            "id": inv.id,
            "invoice_number": inv.display_number,
            "client": str(inv.client) if inv.client else "",
            "status": inv.get_status_display(),
            "date_issued": inv.date_issued.strftime("%d/%m/%Y") if inv.date_issued else "",
            "due_date": inv.due_date.strftime("%d/%m/%Y") if inv.due_date else "",
            "subtotal": str(inv.taxable_base),
            "discount": str(inv.discount_amount),
            "tax_amount": str(inv.tax_amount),
            "total": str(inv.total_amount),
            "payment_status": inv.get_payment_status_display(),
            "reference": inv.reference or "",
            "notes": inv.notes or "",
            "configuration": str(inv.configuration) if inv.configuration else "",
            "items": items,
            "taxes": taxes,
        }
