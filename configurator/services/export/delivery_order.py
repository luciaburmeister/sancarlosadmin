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
        do = self.delivery_order

        items_data = []
        for item in do.items.all():
            items_data.append({
                "item_number": item.item_number,
                "description": item.description,
                "reference": item.reference,
                "quantity": float(item.quantity),
                "unit_price": float(item.unit_price.amount) if item.unit_price else 0,
                "discount_percentage": float(item.discount_percentage),
                "line_total": float(item.line_total.amount) if item.line_total else 0,
            })

        context = {
            "delivery_order": do,
            "number": do.number,
            "date": do.date,
            "status": do.get_status_display(),
            "client": do.client,
            "sales_invoice": do.sales_invoice,
            "project_file": do.project_file,
            "items": items_data,
            "subtotal": float(do.subtotal_amount.amount) if do.subtotal_amount else 0,
            "notes": do.notes,
            # Shipping information
            "package_count": do.package_count,
            "shipping_agency": do.shipping_agency,
            "shipment_number": do.shipment_number,
            "tracking_link": do.tracking_link,
        }

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
