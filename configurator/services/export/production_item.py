from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy
from .context_helpers import get_company_context

if TYPE_CHECKING:
    from production.models import ProductionItem

class ProductionItemExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting an individual ProductionItem.
    """
    def __init__(self, production_item: "ProductionItem"):
        super().__init__(production_item)
        self.production_item = production_item

    def get_context(self) -> Dict[str, Any]:
        ticket = self.production_item.ticket
        context = {
            "item": self.production_item,
            "data": self.production_item.data,
            "planning": self.production_item.planning,
            "ticket": ticket,
        }

        # If the source is a Configuration, we can provide its context as well
        from configurator.models import Configuration
        if isinstance(ticket.source_object, Configuration):
            config = ticket.source_object
            pricing_context = config.get_pricing_context()
            # Simplify items for JSON context
            items_data = []
            for item in config.items.select_related("component").all():
                item_dict = {
                    "id": item.id,
                    "label": item.label,
                    "quantity": float(item.quantity) if item.quantity else 0,
                    "length": float(item.length) if item.length else 0,
                    "note": item.note,
                    "component": {
                        "id": item.component.id if item.component else None,
                        "name": item.component.name if item.component else None,
                        "code": item.component.code if item.component else None,
                        "type": item.component.component_type if item.component else None,
                        "metadata": item.component.metadata if item.component else {},
                    }
                }
                items_data.append(item_dict)

            context.update({
                **pricing_context,
                "config": config,
                "client": config.client,
                "items": items_data,
                "parameters": config.param_values.select_related("parameter"),
                "computed": pricing_context,
            })

        # Use common helper for company and bank information
        company_ctx = get_company_context(self.production_item.company)
        context.update(company_ctx)

        return context

    def get_product(self):
        from configurator.models import Configuration
        ticket = self.production_item.ticket
        if isinstance(ticket.source_object, Configuration):
            return ticket.source_object.product
        return None
