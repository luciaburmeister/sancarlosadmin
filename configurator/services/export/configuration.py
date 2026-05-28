from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy
from django.template import engines
from configurator.utils import safe_eval
from .context_helpers import get_company_context

if TYPE_CHECKING:
    from configurator.models import Configuration

class ConfigurationExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a full Configuration.
    """
    def __init__(self, configuration: "Configuration"):
        super().__init__(configuration)
        self.configuration = configuration

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        pricing_context = self.configuration.get_pricing_context()
        
        # Simplify items for JSON context
        items_data = []
        for item in self.configuration.items.select_related("component").order_by("label").all():
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
                }
            }
            items_data.append(item_dict)

        base_context = {
            **pricing_context,
            "config": self.configuration,
            "client": self.configuration.client,
            "items": items_data,
            "parameters": self.configuration.param_values.select_related("parameter"),
            "component_groups": self.configuration.component_groups.prefetch_related("items").order_by("order"),
            "computed": pricing_context,
            "parameter_pricing_dictionary": pricing_context,
        }

        # Use common helper for company and bank information
        company_ctx = get_company_context(self.configuration.company)
        base_context.update(company_ctx)

        if include_rendered_templates:
            base_context["rendered_templates"] = getattr(self.configuration, "rendered_templates", {})

        return base_context

    def get_product(self):
        return self.configuration.product
