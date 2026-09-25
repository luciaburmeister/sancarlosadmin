from decimal import Decimal
from typing import Any, Dict

from .base import BaseExportStrategy
from .context_helpers import get_company_context


class PurchaseOrderExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a purchasing PurchaseOrder (pedido de compra)
    as a PDF to send to the provider.
    """

    def __init__(self, purchase_order):
        super().__init__(purchase_order)
        self.purchase_order = purchase_order

    def get_context(self, include_rendered_templates: bool = True) -> Dict[str, Any]:
        po = self.purchase_order
        provider = po.provider

        items = []
        # Taxes come from each line's inventory item (same rule as
        # PurchaseOrder.total_amount_with_tax), grouped per tax type and rate
        # so templates can print one row per tax.
        taxes = {}
        lines = po.items.select_related("inventory_item").order_by("pk")
        for position, item in enumerate(lines, start=1):
            line_total = item.line_total.amount if item.line_total else Decimal("0")
            for tax in item.inventory_item.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                entry = taxes.setdefault(key, {
                    "name": tax.tax_type.tax_name,
                    "percentage": float(tax.selected_percentage),
                    "is_retention": tax.tax_type.behavior == "subtract",
                    "base": Decimal("0"),
                    "amount": Decimal("0"),
                })
                entry["base"] += line_total
                entry["amount"] += tax.calculate(line_total)["tax_amount"]

            items.append({
                "item_number": position,
                "description": item.description,
                "reference": item.inventory_item.code,
                "inventory_item": item.inventory_item,
                "quantity": float(item.quantity),
                "unit": item.get_unit_display(),
                "unit_price": float(item.unit_price.amount) if item.unit_price else 0,
                "line_total": float(line_total),
            })

        subtotal = po.total_amount.amount if po.total_amount else Decimal("0")
        tax_amount = sum(
            (-t["amount"] if t["is_retention"] else t["amount"]) for t in taxes.values()
        )

        context = {
            "purchase_order": po,
            "number": po.number,
            "date_ordered": po.date_ordered,
            "status": po.get_status_display(),
            "po_type": po.get_po_type_display(),
            "provider": provider,
            "provider_name": provider.name,
            "provider_contact": provider.contact,
            "provider_phone": provider.phone,
            "provider_email": provider.email,
            "provider_tax_id": provider.tax_identifier,
            "external_reference": po.external_reference,
            "project_file": po.project_file,
            "cost_center": po.cost_center,
            "payment_method": po.get_payment_method_display(),
            "payment_terms_days": po.payment_terms_days,
            "notes": po.notes or "",
            "requested_by": po.requested_by,
            "requested_by_name": (po.requested_by.get_full_name() or po.requested_by.username) if po.requested_by else "",
            "approved_by": po.approved_by,
            "approved_by_name": (po.approved_by.get_full_name() or po.approved_by.username) if po.approved_by else "",
            "approved_at": po.approved_at,
            "items": items,
            "subtotal": float(subtotal),
            "taxes": [
                {**t, "base": float(t["base"]), "amount": float(t["amount"])}
                for t in taxes.values()
            ],
            "tax_amount": float(tax_amount),
            "total": float(subtotal + tax_amount),
            "currency": str(po.total_amount.currency) if po.total_amount else "EUR",
        }

        context.update(get_company_context(po.company))
        return context

    def get_product(self):
        """
        Purchase orders are not linked to a single product.
        Return None so templates with product=NULL can be matched.
        """
        return None
