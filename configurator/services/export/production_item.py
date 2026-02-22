from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy

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
            context.update({
                **pricing_context,
                "config": config,
                "client": config.client,
                "parameters": config.param_values.select_related("parameter"),
                "computed": pricing_context,
            })

        return context

    def get_product(self):
        from configurator.models import Configuration
        ticket = self.production_item.ticket
        if isinstance(ticket.source_object, Configuration):
            return ticket.source_object.product
        return None
