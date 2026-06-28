"""
Sales tools — Clients, Quotes, and Delivery Orders.
"""

from __future__ import annotations

from chatbot.interfaces import BaseTool, ChatContext, ToolExecutionError
from chatbot.registry import register_tool
from datetime import datetime
from django.db import models
from django.db.models import Count, Sum, Q
from configurator.models import Configuration

@register_tool
class ListClients(BaseTool):
    """Search and list clients for the current company."""

    @property
    def name(self) -> str:
        return "list_clients"

    @property
    def description(self) -> str:
        return (
            "List clients (customers) for the company. "
            "Supports optional text search by name, email, or tax identifier. "
            "Returns name, phone, email, tax ID, sale zone, and agent."
        )

    @property
    def required_permission(self) -> str | None:
        return "sales.view_client"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Search term to filter clients by name, email, or tax identifier.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return.",
                    "default": 25,
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from sales.models import Client

        qs = Client.objects.filter(company_id=context.company_id).select_related(
            "sale_zone", "agent"
        )

        search = params.get("search", "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(tax_identifier__icontains=search)
            )

        limit = min(params.get("limit", 25), 100)
        clients = qs.order_by("name")[:limit]

        return {
            "total_count": qs.count(),
            "returned_count": len(clients),
            "clients": [
                {
                    "id": c.id,
                    "name": c.name,
                    "phone": c.phone or "",
                    "email": c.email or "",
                    "tax_identifier": c.tax_identifier or "",
                    "sale_zone": str(c.sale_zone.name) if c.sale_zone else "",
                    "agent": str(c.agent) if c.agent else "",
                    "payment_method": c.get_payment_method_display() if c.payment_method else "",
                    "is_insured": c.is_insured,
                }
                for c in clients
            ],
        }


@register_tool
class GetClientDetail(BaseTool):
    """Get full detail for a single client."""

    @property
    def name(self) -> str:
        return "get_client_detail"

    @property
    def description(self) -> str:
        return (
            "Get detailed information for a specific client, including "
            "addresses, contacts, bank accounts, and financial risk data."
        )

    @property
    def required_permission(self) -> str | None:
        return "sales.view_client"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "integer",
                    "description": "The ID of the client to retrieve.",
                },
            },
            "required": ["client_id"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from sales.models import Client

        try:
            client = Client.objects.select_related("sale_zone", "agent").get(
                pk=params["client_id"], company_id=context.company_id
            )
        except Client.DoesNotExist:
            raise ToolExecutionError(f"Client #{params['client_id']} not found.")

        addresses = [
            {
                "type": addr.get_address_type_display(),
                "street": addr.street,
                "city": addr.city,
                "postal_code": addr.postal_code,
                "province": addr.province,
                "country": str(addr.country),
            }
            for addr in client.addresses.all()
        ]

        contacts = [
            {
                "name": ct.name,
                "position": ct.position,
                "email": ct.email,
                "phone": ct.phone,
            }
            for ct in client.contacts.all()
        ]

        return {
            "id": client.id,
            "name": client.name,
            "phone": client.phone,
            "email": client.email,
            "tax_identifier": client.tax_identifier,
            "address": client.address,
            "sale_zone": str(client.sale_zone) if client.sale_zone else "",
            "agent": str(client.agent) if client.agent else "",
            "agent_commission": str(client.agent.commission_percentage) if client.agent else "0",
            "payment_method": client.get_payment_method_display(),
            "is_insured": client.is_insured,
            "max_risk": str(client.max_risk) if client.max_risk else "0",
            "current_risk": str(client.current_risk) if client.current_risk else "0",
            "addresses": addresses,
            "contacts": contacts,
        }


@register_tool
class ListQuotes(BaseTool):
    """List quotes with optional filters."""

    @property
    def name(self) -> str:
        return "list_quotes"

    @property
    def description(self) -> str:
        return (
            "List sales quotes (presupuestos) for the company. "
            "Filter by status (draft, sent, accepted, rejected) or client. "
            "Returns quote summary with project name, client, status, and totals."
        )

    @property
    def required_permission(self) -> str | None:
        return "sales.view_quote"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: draft, sent, accepted, rejected.",
                    "enum": ["draft", "sent", "accepted", "rejected"],
                },
                "client_id": {
                    "type": "integer",
                    "description": "Filter quotes by client ID.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results.",
                    "default": 25,
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from sales.models import Quote

        qs = Quote.objects.filter(company_id=context.company_id).select_related("client")

        if status := params.get("status"):
            qs = qs.filter(status=status)
        if client_id := params.get("client_id"):
            qs = qs.filter(client_id=client_id)

        limit = min(params.get("limit", 25), 100)
        quotes = qs.order_by("-created_at")[:limit]

        return {
            "total_count": qs.count(),
            "returned_count": len(quotes),
            "quotes": [
                {
                    "id": q.id,
                    "project_name": q.project_name or f"Quote #{q.id}",
                    "client": str(q.client) if q.client else "",
                    "status": q.get_status_display(),
                    "date": q.date.strftime("%d/%m/%Y") if q.date else "",
                    "expires_at": q.expires_at.strftime("%d/%m/%Y") if q.expires_at else "",
                    "subtotal": str(q.subtotal_price),
                    "tax": str(q.tax_amount),
                    "total": str(q.total_price),
                }
                for q in quotes
            ],
        }


@register_tool
class GetQuoteDetail(BaseTool):
    """Get full quote detail with configurations and items."""

    @property
    def name(self) -> str:
        return "get_quote_detail"

    @property
    def description(self) -> str:
        return (
            "Get detailed information for a specific quote, including "
            "its line items, linked configurations, tax breakdown, and totals."
        )

    @property
    def required_permission(self) -> str | None:
        return "sales.view_quote"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "quote_id": {
                    "type": "integer",
                    "description": "The ID of the quote to retrieve.",
                },
            },
            "required": ["quote_id"],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        from sales.models import Quote

        try:
            quote = Quote.objects.select_related("client", "project_file").get(
                pk=params["quote_id"], company_id=context.company_id
            )
        except Quote.DoesNotExist:
            raise ToolExecutionError(f"Quote #{params['quote_id']} not found.")

        items = [
            {
                "material": str(item.material) if item.material_id else "",
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "discount": str(item.discount),
                "line_total": str(item.line_total),
            }
            for item in quote.items.select_related("material").all()
        ]

        configurations = [
            {
                "id": cfg.id,
                "product": str(cfg.product) if cfg.product_id else "",
                "status": cfg.get_status_display(),
                "unit_price": str(cfg.unit_price),
                "subtotal": str(cfg.subtotal_price),
                "total": str(cfg.total_price),
            }
            for cfg in quote.configurations.select_related("product").all()
        ]

        return {
            "id": quote.id,
            "project_name": quote.project_name,
            "client": str(quote.client) if quote.client else "",
            "project_file": str(quote.project_file) if quote.project_file else "",
            "status": quote.get_status_display(),
            "date": quote.date.strftime("%d/%m/%Y") if quote.date else "",
            "expires_at": quote.expires_at.strftime("%d/%m/%Y") if quote.expires_at else "",
            "subtotal": str(quote.subtotal_price),
            "tax_amount": str(quote.tax_amount),
            "tax_breakdown": quote.tax_breakdown or [],
            "total": str(quote.total_price),
            "items": items,
            "configurations": configurations,
        }


@register_tool
class ListDeliveryOrders(BaseTool):
    """List delivery orders."""

    @property
    def name(self) -> str:
        return "list_delivery_orders"

    @property
    def description(self) -> str:
        return (
            "List delivery orders (albaranes). "
            "Filter by client or status (draft, sent, delivered, cancelled)."
        )

    @property
    def required_permission(self) -> str | None:
        return "sales.view_deliveryorder"

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
                    "description": "Filter by status.",
                    "enum": ["draft", "sent", "delivered", "cancelled"],
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
        from sales.models import DeliveryOrder

        qs = DeliveryOrder.objects.filter(
            company_id=context.company_id
        ).select_related("client")

        if client_id := params.get("client_id"):
            qs = qs.filter(client_id=client_id)
        if status := params.get("status"):
            qs = qs.filter(status=status)

        limit = min(params.get("limit", 25), 100)
        orders = qs.order_by("-date")[:limit]

        return {
            "total_count": qs.count(),
            "returned_count": len(orders),
            "delivery_orders": [
                {
                    "id": o.id,
                    "number": o.number or "Draft",
                    "client": str(o.client),
                    "date": o.date.strftime("%d/%m/%Y") if o.date else "",
                    "status": o.get_status_display(),
                    "items_count": o.items.count(),
                }
                for o in orders
            ],
        }


@register_tool
class TopDemandedProducts(BaseTool):
    """Rank product templates by demand based on configuration count and revenue."""

    @property
    def name(self) -> str:
        return "top_demanded_products"

    @property
    def description(self) -> str:
        return (
            "Rank product templates (products) by demand. "
            "Shows how many configurations each product has, total revenue generated, "
            "and the percentage of accepted vs. total quotes. "
            "Filter by date range or configuration status. "
            "Useful for identifying best-selling or most-quoted products."
        )

    @property
    def required_permission(self) -> str | None:
        return "configurator.view_configuration"

    def get_parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": (
                        "Start date (YYYY-MM-DD). Only count configurations "
                        "created on or after this date."
                    ),
                },
                "date_to": {
                    "type": "string",
                    "description": (
                        "End date (YYYY-MM-DD). Only count configurations "
                        "created on or before this date."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Filter by configuration status: "
                        "draft, completed, converted, shop, production_done. "
                        "Leave empty to include all statuses."
                    ),
                    "enum": ["draft", "completed", "converted", "shop", "production_done"],
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of products to return.",
                    "default": 15,
                },
            },
            "required": [],
        }

    def execute(self, context: ChatContext, params: dict) -> dict:
        

        qs = Configuration.objects.filter(
            company_id=context.company_id,
            product__isnull=False,
        )

        if date_from := params.get("date_from"):
            try:
                qs = qs.filter(created_at__date__gte=datetime.strptime(date_from, "%Y-%m-%d").date())
            except ValueError:
                raise ToolExecutionError(f"Invalid date_from format: {date_from}. Use YYYY-MM-DD.")

        if date_to := params.get("date_to"):
            try:
                qs = qs.filter(created_at__date__lte=datetime.strptime(date_to, "%Y-%m-%d").date())
            except ValueError:
                raise ToolExecutionError(f"Invalid date_to format: {date_to}. Use YYYY-MM-DD.")

        if status := params.get("status"):
            qs = qs.filter(status=status)

        limit = min(params.get("limit", 15), 50)

        ranked = (
            qs.values("product__id", "product__name")
            .annotate(
                configuration_count=Count("id"),
                total_revenue=Sum("subtotal_price", output_field=models.DecimalField()),
                accepted_count=Count(
                    "id",
                    filter=Q(status__in=["converted", "shop", "production_done"]),
                ),
            )
            .order_by("-configuration_count")[:limit]
        )

        products = []
        for row in ranked:
            total = row["configuration_count"]
            accepted = row["accepted_count"]
            acceptance_rate = round((accepted / total) * 100, 1) if total else 0

            products.append({
                "product_id": row["product__id"],
                "product_name": row["product__name"],
                "configuration_count": total,
                "accepted_count": accepted,
                "acceptance_rate_pct": acceptance_rate,
                "total_revenue": str(row["total_revenue"]),
            })

        return {
            "total_products_with_configs": qs.values("product").distinct().count(),
            "returned_count": len(products),
            "products": products,
        }
