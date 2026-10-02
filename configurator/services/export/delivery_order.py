from typing import Any, Dict

from .base import BaseExportStrategy
from .context_helpers import get_company_context


class DeliveryOrderExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a Delivery Order (Albarán de Entrega).
    """

    def __init__(self, delivery_order):
        super().__init__(delivery_order)
        self.delivery_order = delivery_order

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        from decimal import Decimal

        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Prefetch

        from inventory.models import ItemIdentityCode
        from sales.models import Quote

        do = self.delivery_order
        quote_ct = ContentType.objects.get_for_model(Quote)

        items_data = []
        financing_discount_amount = Decimal("0.00")
        # Codes default to newest-first; on a printed document they read better
        # in code order.
        code_prefetch = Prefetch(
            "identity_codes",
            queryset=ItemIdentityCode.objects.order_by("code"),
        )
        # Component lines (materialised bill of materials) print right under
        # their parent, indented by depth and without a price of their own.
        # Templates get the same lines two ways: `items` flat in print order
        # (each one knowing its depth and its parent) and `line_tree`, the
        # top-level lines with their components nested under `components`, for
        # templates that want to draw each bill of materials as a block.
        lines = list(do.items.prefetch_related(code_prefetch).order_by("pk"))
        children = {}
        for line in lines:
            children.setdefault(line.parent_line_id, []).append(line)

        top_level = []

        def walk(parent_id, depth, parent_data, root_data):
            nonlocal financing_discount_amount
            siblings = children.get(parent_id, [])
            built = []
            for position, item in enumerate(siblings, start=1):
                # Quote-sourced items are legacy financing lines.
                is_discount_line = item.source_content_type_id == quote_ct.id
                if is_discount_line and item.line_total:
                    financing_discount_amount += -item.line_total.amount
                identity_codes = [c.code for c in item.identity_codes.all()]
                data = {
                    "item_number": item.item_number,
                    "description": item.description,
                    "reference": item.reference,
                    "quantity": float(item.quantity),
                    "unit_price": float(item.unit_price.amount) if item.unit_price else 0,
                    "discount_percentage": float(item.discount_percentage),
                    "line_total": float(item.line_total.amount) if item.line_total else 0,
                    "is_discount_line": is_discount_line,
                    "is_component": depth > 0,
                    "depth": depth,
                    # Indentation is precomputed: Django templates cannot
                    # multiply depth by a step width on their own.
                    "indent_px": depth * 14,
                    "parent_item_number": parent_data["item_number"] if parent_data else "",
                    "parent_description": parent_data["description"] if parent_data else "",
                    "parent_reference": parent_data["reference"] if parent_data else "",
                    # The priced line this component ultimately belongs to,
                    # whatever the nesting depth.
                    "root_item_number": root_data["item_number"] if root_data else item.item_number,
                    "root_description": root_data["description"] if root_data else item.description,
                    "is_last_child": position == len(siblings),
                    "identity_codes": identity_codes,
                    "has_identity_codes": bool(identity_codes),
                }
                items_data.append(data)
                data["components"] = walk(
                    item.pk, depth + 1, data, root_data or data
                )
                data["has_components"] = bool(data["components"])
                # Every descendant, flattened, for templates that print a
                # parent row followed by its whole bill of materials.
                data["all_components"] = [
                    descendant
                    for child in data["components"]
                    for descendant in [child, *child["all_components"]]
                ]
                data["component_count"] = len(data["all_components"])
                built.append(data)
            return built

        top_level = walk(None, 0, None, None)

        # ``lines_total`` is the raw sum of line totals; ``subtotal`` is what
        # templates print as "Base": the taxable base after the header
        # discount, so Base + taxes = Total on the printed document (same
        # contract as the sales invoice export).
        lines_total = do.subtotal_amount.amount if do.subtotal_amount else Decimal("0")
        discount = do.discount_amount.amount if do.discount_amount else Decimal("0")
        subtotal = lines_total - discount
        # Gross figure before the financing lines netted themselves out.
        gross_subtotal = lines_total + financing_discount_amount
        tax_amount = do.tax_amount.amount if do.tax_amount else Decimal("0")
        total = do.total_amount.amount if do.total_amount else Decimal("0")
        # Legacy delivery orders predate tax/total fields: print subtotal as total
        if total == 0 and tax_amount == 0 and discount == 0:
            total = subtotal

        context = {
            "delivery_order": do,
            "number": do.number,
            "date": do.date,
            "status": do.get_status_display(),
            "client": do.client,
            "sales_invoice": do.sales_invoice,
            "project_file": do.project_file,
            "items": items_data,
            "line_tree": top_level,
            "has_components": any(i["is_component"] for i in items_data),
            "has_identity_codes": any(i["has_identity_codes"] for i in items_data),
            "subtotal": float(subtotal),
            "lines_total": float(lines_total),
            "gross_subtotal": float(gross_subtotal),
            "discount_amount": float(discount),
            "discount_breakdown": do.discount_breakdown or [],
            "tax_amount": float(tax_amount),
            "total": float(total),
            "has_financing_discount": financing_discount_amount > 0,
            "financing_discount_amount": float(financing_discount_amount),
            "notes": do.notes,
            # Shipping information
            "delivery_address": do.delivery_address,
            "shipping_method": do.get_shipping_method_display(),
            "planned_delivery_date": do.planned_delivery_date,
            "dispatched_at": do.dispatched_at,
            "delivered_at": do.delivered_at,
            "received_by": do.received_by,
            "package_count": do.package_count,
            "shipping_agency": do.shipping_agency,
            "shipment_number": do.shipment_number,
            "tracking_link": do.tracking_link,
        }

        context["taxes"] = [
            {
                "name": tax.tax_type.tax_name,
                "percentage": float(tax.selected_percentage),
                "amount": float(tax.calculated_amount.amount) if tax.calculated_amount else 0,
                "base": float(tax.base_amount.amount) if tax.base_amount else 0,
                "is_retention": tax.tax_type.behavior == "subtract",
            }
            for tax in do.applied_taxes.select_related("tax_type").order_by("order")
        ]

        company_ctx = get_company_context(do.company)
        context.update(company_ctx)

        return context

    def get_product(self):
        """
        If linked to a configuration, return its product so product-specific
        templates can be matched. General templates match otherwise.
        """
        if self.delivery_order.configuration:
            return self.delivery_order.configuration.product
        return None
