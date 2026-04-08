from typing import Any, Dict
from .base import BaseExportStrategy
from decimal import Decimal
from .context_helpers import get_company_context

class ReceivalSlipExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a Receival Slip (Albarán de Recibida).
    """
    def __init__(self, receival_slip):
        super().__init__(receival_slip)
        self.receival_slip = receival_slip

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        items_data = []
        for item in self.receival_slip.items.all():
            items_data.append({
                "description": item.description,
                "quantity": float(item.quantity),
                "measurements": item.measurements,
                "notes": item.notes,
            })

        context = {
            "receival_slip": self.receival_slip,
            "configuration": self.receival_slip.configuration,
            "client": self.receival_slip.configuration.client if self.receival_slip.configuration else None,
            "items": items_data,
            "reference": self.receival_slip.reference,
            "date": self.receival_slip.date,
            "notes": self.receival_slip.notes,
        }

        company_ctx = get_company_context(self.receival_slip.company)
        context.update(company_ctx)

        return context

    def get_product(self):
        """
        If linked to a configuration, return its product.
        """
        if self.receival_slip.configuration:
            return self.receival_slip.configuration.product
        return None
