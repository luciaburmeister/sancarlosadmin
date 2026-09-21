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
                "price_total": float(group.price_total),
                "configurations": member_configs,
            })

        ungrouped_configs = [
            config_contexts[c.pk]
            for c in all_configs
            if c.quote_group_id is None
        ]
        inventory_items_data = []
        for inv_item in (
            self.quote.inventory_items
            .select_related("inventory_item")
            .order_by("order", "pk")
        ):
            # A kit-type item (e.g. a solar water heater sold as one line) can
            # have its own Bill of Materials (InventoryItem.bom_lines, the
            # same "Componente"/"Cantidad" list shown on the item's own admin
            # page) -- exposed here so a quote template can optionally list
            # what's inside it, same as sales_invoice.py exposes item.reference.
            components = []
            if inv_item.inventory_item_id:
                components = [
                    {
                        "code": bom_line.component.code,
                        "name": str(bom_line.component),
                        "quantity": bom_line.quantity,
                    }
                    for bom_line in inv_item.inventory_item.bom_lines.select_related("component")
                ]
            inventory_items_data.append({
                "id": inv_item.pk,
                "name": str(inv_item.inventory_item) if inv_item.inventory_item_id else "",
                "code": inv_item.inventory_item.code if inv_item.inventory_item_id and hasattr(inv_item.inventory_item, "code") else "",
                "quantity": float(inv_item.quantity),
                "unit_price": float(inv_item.unit_price.amount) if inv_item.unit_price else 0,
                "discount": float(inv_item.discount),
                "line_total": float(inv_item.line_total.amount) if inv_item.line_total else 0,
                "total_with_tax": float(inv_item.total_with_tax.amount) if inv_item.total_with_tax else 0,
                "components": components,
                "has_components": len(components) > 0,
            })

        # Sums for the export summary block
        lines_subtotal = sum(
            (Decimal(str(item["line_total"])) for item in inventory_items_data),
            Decimal("0.00"),
        )
        configurations_subtotal = sum(
            (Decimal(str(c["subtotal"])) for c in configurations_data),
            Decimal("0.00"),
        )
        line_items_discount_total = sum(
            (
                Decimal(str(item["unit_price"])) * Decimal(str(item["quantity"]))
                - Decimal(str(item["line_total"]))
                for item in inventory_items_data
            ),
            Decimal("0.00"),
        )
        configurations_discount_total = sum(
            (
                abs(Decimal((config.pricing_breakdown or {}).get("discount_total", "0")))
                for config in all_configs
            ),
            Decimal("0.00"),
        )
        basket_discount_total = self.quote.discount_amount.amount if self.quote.discount_amount else Decimal("0.00")
        discounts_total = line_items_discount_total + configurations_discount_total + basket_discount_total

        discount_breakdown = self.quote.discount_breakdown or []
        financing_discount_amount = sum(
            (
                abs(Decimal(entry["amount"]))
                for entry in discount_breakdown
                if entry.get("kind") == "financing" and entry.get("level") == "basket"
            ),
            Decimal("0.00"),
        )

        context = {
            "quote": self.quote,
            "client": self.quote.client,
            # Backwards-compatible flat list
            "configurations": configurations_data,
            # Group-aware access
            "groups": groups_data,
            "ungrouped_configurations": ungrouped_configs,
            "has_groups": len(groups_data) > 0,
            # Inventory / sale items
            "inventory_items": inventory_items_data,
            "has_inventory_items": len(inventory_items_data) > 0,
            "subtotal_price": float(self.quote.subtotal_price.amount),
            "discount_amount": float(self.quote.discount_amount.amount) if self.quote.discount_amount else 0,
            "discount_breakdown": discount_breakdown,
            "discounted_subtotal": float(self.quote.discounted_subtotal.amount) if self.quote.discounted_subtotal else 0,
            "tax_amount": float(self.quote.tax_amount.amount),
            "total_price": float(self.quote.total_price.amount),
            "tax_breakdown": self.quote.tax_breakdown,
            "config_count": len(configurations_data),
            # Summary sums
            "lines_subtotal": float(lines_subtotal),
            "configurations_subtotal": float(configurations_subtotal),
            "line_items_discount_total": float(line_items_discount_total),
            "configurations_discount_total": float(configurations_discount_total),
            "discounts_total": float(discounts_total),
            "has_financing_discount": financing_discount_amount > 0,
            "financing_discount_amount": float(financing_discount_amount),
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
