from typing import Any, Dict

from .base import BaseExportStrategy
from .context_helpers import get_company_context


class QuoteRequestExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a purchasing QuoteRequest (solicitud de cotización)
    as a PDF to send to providers.
    """

    def __init__(self, quote_request):
        super().__init__(quote_request)
        self.quote_request = quote_request

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        qr = self.quote_request

        items = [
            {
                "description": item.description,
                "quantity": item.quantity,
                "unit": item.get_unit_display(),
                "inventory_item": item.inventory_item,
            }
            for item in qr.items.select_related("inventory_item").all()
        ]

        context = {
            "quote_request": qr,
            "number": qr.number,
            "date_requested": qr.date_requested,
            "status": qr.get_status_display(),
            "notes": qr.notes or "",
            "requested_by": qr.requested_by,
            "requested_by_name": qr.requested_by.get_full_name() or qr.requested_by.username if qr.requested_by else "",
            "items": items,
        }

        context.update(get_company_context(qr.company))
        return context

    def get_product(self):
        """
        Quote requests are not linked to a single product.
        Return None so templates with product=NULL can be matched.
        """
        return None
