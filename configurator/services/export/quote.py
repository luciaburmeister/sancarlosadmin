from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy
from decimal import Decimal
from .context_helpers import get_company_context

if TYPE_CHECKING:
    from sales.models import Quote

class QuoteExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a full Quote with all its configurations.
    """
    def __init__(self, quote: "Quote"):
        super().__init__(quote)
        self.quote = quote

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        from configurator.services.export import ConfigurationExportService
        
        configurations_data = []
        for config in self.quote.configurations.all():
            strategy = ConfigurationExportService.get_strategy(config)
            config_context = strategy.get_context(include_rendered_templates=include_rendered_templates)
            config_context["subtotal"] = float(config.subtotal_price.amount)
            config_context["total"] = float(config.total_price.amount)
            pb = config.pricing_breakdown or {}
            config_context["discounts"] = [
                l for l in pb.get("lines", []) if l.get("type") == "discount"
            ]
            
            configurations_data.append(config_context)

        context = {
            "quote": self.quote,
            "client": self.quote.client,
            "configurations": configurations_data,
            "subtotal_price": float(self.quote.subtotal_price.amount),
            "tax_amount": float(self.quote.tax_amount.amount),
            "total_price": float(self.quote.total_price.amount),
            "tax_breakdown": self.quote.tax_breakdown,
            "config_count": len(configurations_data),
        }

        # Use common helper for company and bank information
        company_ctx = get_company_context(self.quote.company)
        context.update(company_ctx)

        return context

    def get_product(self):
        """
        Quotes don't have a single product template, but we can return None
        or a generic product if needed. For now, returning None allows
        general templates to be matched.
        """
        return None
