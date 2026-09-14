from typing import Any, Dict

from .base import BaseExportStrategy
from .context_helpers import get_company_context


class DeliveryOrderExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a Delivery Order (Albarán de Entrega).
    """

    def __init__(self, delivery_order):
        super().__init__(delivery_order)
        self.delivery_order = delivery_order

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        from decimal import Decimal

        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Prefetch

        from inventory.models import ItemIdentityCode
        from sales.models import Quote

        do = self.delivery_order
        quote_ct = ContentType.objects.get_for_model(Quote)

        items_data = []
        financing_discount_amount = Decimal("0.00")
        # Codes default to newest-first; on a printed document they read better
        # in code order.
        code_prefetch = Prefetch(
            "identity_codes",
            queryset=ItemIdentityCode.objects.order_by("code"),
        )
        for item in do.items.prefetch_related(code_prefetch):
            # Quote-sourced items are legacy financing lines.
            is_discount_line = item.source_content_type_id == quote_ct.id
            if is_discount_line and item.line_total:
                financing_discount_amount += -item.line_total.amount
            identity_codes = [c.code for c in item.identity_codes.all()]
            items_data.append({
                "item_number": item.item_number,
                "description": item.description,
                "reference": item.reference,
                "quantity": float(item.quantity),
                "unit_price": float(item.unit_price.amount) if item.unit_price else 0,
                "discount_percentage": float(item.discount_percentage),
                "line_total": float(item.line_total.amount) if item.line_total else 0,
                "is_discount_line": is_discount_line,
                "identity_codes": identity_codes,
                "has_identity_codes": bool(identity_codes),
            })

        subtotal = do.subtotal_amount.amount if do.subtotal_amount else Decimal("0")
        discount = do.discount_amount.amount if do.discount_amount else Decimal("0")
        tax_amount = do.tax_amount.amount if do.tax_amount else Decimal("0")
        total = do.total_amount.amount if do.total_amount else Decimal("0")
        # Legacy delivery orders predate tax/total fields: print subtotal as total
        if total == 0 and tax_amount == 0 and discount == 0:
            total = subtotal

        context = {
            "delivery_order": do,
            "number": do.number,
            "date": do.date,
            "status": do.get_status_display(),
            "client": do.client,
            "sales_invoice": do.sales_invoice,
            "project_file": do.project_file,
            "items": items_data,
            "has_identity_codes": any(i["has_identity_codes"] for i in items_data),
            "subtotal": float(subtotal),
            "discount_amount": float(discount),
            "discount_breakdown": do.discount_breakdown or [],
            "tax_amount": float(tax_amount),
            "total": float(total),
            "has_financing_discount": financing_discount_amount > 0,
            "financing_discount_amount": float(financing_discount_amount),
            "notes": do.notes,
            # Shipping information
            "package_count": do.package_count,
            "shipping_agency": do.shipping_agency,
            "shipment_number": do.shipment_number,
            "tracking_link": do.tracking_link,
        }

        context["taxes"] = [
            {
                "name": tax.tax_type.tax_name,
                "percentage": float(tax.selected_percentage),
                "amount": float(tax.calculated_amount.amount) if tax.calculated_amount else 0,
                "base": float(tax.base_amount.amount) if tax.base_amount else 0,
                "is_retention": tax.tax_type.behavior == "subtract",
            }
            for tax in do.applied_taxes.select_related("tax_type").order_by("order")
        ]

        company_ctx = get_company_context(do.company)
        context.update(company_ctx)

        return context

    def get_product(self):
        """
        If linked to a configuration, return its product so product-specific
        templates can be matched. General templates match otherwise.
        """
        if self.delivery_order.configuration:
            return self.delivery_order.configuration.product
        return None
