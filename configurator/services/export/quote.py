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

    def _build_config_context(self, config, include_rendered_templates):
        from configurator.services.export import ConfigurationExportService
        strategy = ConfigurationExportService.get_strategy(config)
        config_context = strategy.get_context(include_rendered_templates=include_rendered_templates)
        config_context["subtotal"] = float(config.subtotal_price.amount)
        config_context["total"] = float(config.total_price.amount)
        pb = config.pricing_breakdown or {}
        config_context["discounts"] = [
            l for l in pb.get("lines", []) if l.get("type") == "discount"
        ]
        return config_context

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        # Build a dict of config_id -> config_context for reuse
        all_configs = list(self.quote.configurations.select_related("quote_group").all())
        config_contexts = {
            config.pk: self._build_config_context(config, include_rendered_templates)
            for config in all_configs
        }

        # Flat list (backwards compatible)
        configurations_data = list(config_contexts.values())

        # Group-aware lists
        groups_data = []
        for group in self.quote.configuration_groups.prefetch_related("configurations").order_by("order"):
            member_configs = [
                config_contexts[c.pk]
                for c in group.configurations.all()
                if c.pk in config_contexts
            ]
            groups_data.append({
                "id": group.pk,
                "name": group.name,
                "notes": group.notes,
                "order": group.order,
                "subtotal": float(group.subtotal),
                "total": float(group.total),
                "unit_price": float(group.unit_price),
                "configurations": member_configs,
            })

        ungrouped_configs = [
            config_contexts[c.pk]
            for c in all_configs
            if c.quote_group_id is None
        ]

        context = {
            "quote": self.quote,
            "client": self.quote.client,
            # Backwards-compatible flat list
            "configurations": configurations_data,
            # Group-aware access
            "groups": groups_data,
            "ungrouped_configurations": ungrouped_configs,
            "has_groups": len(groups_data) > 0,
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
