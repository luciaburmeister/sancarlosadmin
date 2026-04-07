from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy
from invoices.models import AppliedTax
from django.contrib.contenttypes.models import ContentType
from .context_helpers import get_company_context
if TYPE_CHECKING:
    from invoices.models import SalesInvoice

class SalesInvoiceExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a SalesInvoice.
    """
    def __init__(self, sales_invoice: "SalesInvoice"):
        super().__init__(sales_invoice)
        self.sales_invoice = sales_invoice

    def get_context(self) -> Dict[str, Any]:
        # Items for JSON context
        items_data = []
        for item in self.sales_invoice.items.all():
            item_dict = {
                "id": item.id,
                "description": item.description,
                "quantity": float(item.quantity) if item.quantity else 0,
                "unit_price": float(item.unit_price.amount) if item.unit_price else 0,
                "line_total": float(item.line_total.amount) if item.line_total else 0,
                "discount_amount": float(item.discount_amount.amount) if item.discount_amount else 0,
            }
            items_data.append(item_dict)


        client = self.sales_invoice.client
        client_data = {
            "name": client.name,
            "tax_identifier": client.tax_identifier,
            "phone": client.phone,
            "email": client.email,
            "address": client.address,
        }

        context = {
            "invoice": self.sales_invoice,
            "client": client,
            "client_data": client_data,
            "items": items_data,
            "invoice_number": self.sales_invoice.invoice_number,
            "date_issued": self.sales_invoice.date_issued,
            "due_date": self.sales_invoice.due_date,
            "subtotal": float(self.sales_invoice.subtotal_amount.amount),
            "tax_amount": float(self.sales_invoice.tax_amount.amount),
            "total_amount": float(self.sales_invoice.total_amount.amount),
            "reference": self.sales_invoice.reference,
        }

        # Use common helper for company and bank information
        company_ctx = get_company_context(self.sales_invoice.company)
        context.update(company_ctx)

        # Add detailed taxes
        
        ct = ContentType.objects.get_for_model(self.sales_invoice)
        applied_taxes = AppliedTax.objects.filter(
            content_type=ct, 
            object_id=self.sales_invoice.pk
        ).select_related("tax_type").order_by("order")
        
        context["taxes"] = [
            {
                "name": tax.tax_type.tax_name,
                "percentage": float(tax.selected_percentage),
                "amount": float(tax.calculated_amount.amount) if tax.calculated_amount else 0,
                "base": float(tax.base_amount.amount) if tax.base_amount else 0,
            }
            for tax in applied_taxes
        ]

        
        
        if self.sales_invoice.configuration:
            from .configuration import ConfigurationExportStrategy
            config_strategy = ConfigurationExportStrategy(self.sales_invoice.configuration)
            config_context = config_strategy.get_context()
            config_context.update(context)
            return config_context

        return context

    def get_product(self):
        if self.sales_invoice.configuration:
            return self.sales_invoice.configuration.product
        return None
