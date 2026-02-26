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
        return {
            **pricing_context,
            "config": self.configuration,
            "client": self.configuration.client,
            "items": self.configuration.items.all(),
            "parameters": self.configuration.param_values.select_related("parameter"),
            "component_groups": self.configuration.component_groups.order_by("order"),
            "computed": pricing_context,
            "parameter_pricing_dictionary": pricing_context,
        }

    def get_product(self):
        return self.configuration.product
