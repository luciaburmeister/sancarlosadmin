from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy

if TYPE_CHECKING:
    from configurator.models import Configuration

class ConfigurationExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a full Configuration.
    """
    def __init__(self, configuration: "Configuration"):
        super().__init__(configuration)
        self.configuration = configuration

    def get_context(self) -> Dict[str, Any]:
        pricing_context = self.configuration.get_pricing_context()
        
        # Simplify items for JSON context
        items_data = []
        for item in self.configuration.items.select_related("component").all():
            item_dict = {
                "id": item.id,
                "label": item.label,
                "quantity": float(item.quantity) if item.quantity else 0,
                "length": float(item.length) if item.length else 0,
                "note": item.note,
                "production_stages": item.production_stages,
                "component": {
                    "id": item.component.id if item.component else None,
                    "name": item.component.name if item.component else None,
                    "code": item.component.code if item.component else None,
                    "type": item.component.component_type if item.component else None,
                    "metadata": item.component.metadata if item.component else {},
                }
            }
            items_data.append(item_dict)

        return {
            **pricing_context,
            "config": self.configuration,
            "client": self.configuration.client,
            "items": items_data,
            "parameters": self.configuration.param_values.select_related("parameter"),
            "component_groups": self.configuration.component_groups.order_by("order"),
            "computed": pricing_context,
            "parameter_pricing_dictionary": pricing_context,
        }

    def get_product(self):
        return self.configuration.product
