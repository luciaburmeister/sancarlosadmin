from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict
from .base import BaseExportStrategy
from invoices.models import AppliedTax
from django.contrib.contenttypes.models import ContentType
from .context_helpers import get_company_context
from invoices.services.sales_invoice_payment import effective_payment_method_display
if TYPE_CHECKING:
    from invoices.models import SalesInvoice

class SalesInvoiceExportStrategy(BaseExportStrategy):
    """
    Strategy for exporting a SalesInvoice.
    """
    def __init__(self, sales_invoice: "SalesInvoice"):
        super().__init__(sales_invoice)
        self.sales_invoice = sales_invoice

    @staticmethod
    def _quote_id_from_source(source_id: str):
        from configurator.models import Configuration
        from sales.models import QuoteItem, QuoteInventoryItem
        from invoices.services.discounts import (
            DISCOUNT_LINE_PREFIX,
            is_discount_line_source,
        )

        # A materialized discount line names its quote directly.
        if is_discount_line_source(source_id):
            try:
                return int(source_id[len(DISCOUNT_LINE_PREFIX):])
            except ValueError:
                return None

        # Maps source_id prefix → model that holds a quote_id FK
        prefix_to_model = {
            "config_":     Configuration,
            "quote_item_": QuoteItem,
            "inv_item_":   QuoteInventoryItem,
        }

        for prefix, model in prefix_to_model.items():
            if source_id.startswith(prefix):
                pk = source_id[len(prefix):]
                try:
                    return model.objects.values_list("quote_id", flat=True).get(pk=pk)
                except (model.DoesNotExist, ValueError):
                    return None

        return None

    def _delivery_order_contexts(self):
        """
        The delivery orders billed by this invoice, each one carrying the very
        same context its own export template receives (lines, line_tree,
        identity codes, taxes, shipping data). An invoice template can then
        print its albaranes inline without a second export.
        """
        from .delivery_order import DeliveryOrderExportStrategy

        delivery_orders = (
            self.sales_invoice.delivery_orders
            .select_related(
                "client", "company", "configuration", "project_file",
                "delivery_address", "shipping_agency",
            )
            .order_by("date", "pk")
        )

        contexts = []
        for delivery_order in delivery_orders:
            do_context = DeliveryOrderExportStrategy(delivery_order).get_context()
            # The company block already sits at the top level of the invoice
            # context; repeating it per albarán would only bloat the
            # serialized context with the same data.
            for key in ("company", "company_data", "bank_account"):
                do_context.pop(key, None)
            contexts.append(do_context)

        return contexts

    def get_context(self) -> Dict[str, Any]:
        from invoices.services.discounts import is_discount_line_source

        # Discount lines only exist on invoices created before financing
        # moved into the header discount.
        items_data = []
        financing_discount_amount = Decimal("0.00")
        for item in self.sales_invoice.items.all():

            is_discount_line = is_discount_line_source(item.source_id)
            if is_discount_line and item.line_total:
                financing_discount_amount += -Decimal(str(item.line_total.amount))
            item_dict = {
                "id": item.id,
                "description": item.description,
                "reference": item.reference,
                "quote_name": item.quote_name,
                "quote_id": self._quote_id_from_source(item.source_id),
                "quantity": float(item.quantity) if item.quantity else 0,
                "unit_price": float(item.unit_price.amount) if item.unit_price else 0,
                "line_total": float(item.line_total.amount) if item.line_total else 0,
                "discount_percentage": float(item.discount_percentage or 0),
                "discount_amount": float(item.discount_amount.amount) if item.discount_amount else 0,
                "is_discount_line": is_discount_line,
            }
            items_data.append(item_dict)


        subtotal = Decimal(str(self.sales_invoice.subtotal_amount.amount)) if self.sales_invoice.subtotal_amount else Decimal("0.00")
        discount = Decimal(str(self.sales_invoice.discount_amount.amount)) if self.sales_invoice.discount_amount else Decimal("0.00")
        # Invoice-level discount as a percentage of the line subtotal. The
        # invoice stores only the amount (rules can cascade), so the effective
        # rate is derived from it — against the gross subtotal, since the
        # financing lines already netted themselves out of it.
        gross_subtotal = subtotal + financing_discount_amount
        discount_percentage = (
            float((discount / gross_subtotal * Decimal("100")).quantize(Decimal("0.01")))
            if gross_subtotal > 0
            else 0.0
        )

        client = self.sales_invoice.client
        payment_method_display = effective_payment_method_display(
            sales_invoice=self.sales_invoice,
            client=client,
        )
        client_data = {
            "name": client.name,
            "tax_identifier": client.tax_identifier,
            "phone": client.phone,
            "email": client.email,
            "address": client.address,
            "payment_method_display": payment_method_display,
        }

        context = {
            "invoice": self.sales_invoice,
            "client": client,
            "client_data": client_data,
            "payment_method_display": payment_method_display,
            "items": items_data,
            "invoice_number": self.sales_invoice.invoice_number,
            "date_issued": self.sales_invoice.date_issued,
            "due_date": self.sales_invoice.due_date,
            "subtotal": float(subtotal),
            "discount_amount": float(discount),
            "discount_percentage": discount_percentage,
            "discount_breakdown": self.sales_invoice.discount_breakdown or [],
            "has_financing_discount": financing_discount_amount > 0,
            "financing_discount_amount": float(financing_discount_amount),
            "tax_amount": float(self.sales_invoice.tax_amount.amount),
            "total_amount": float(self.sales_invoice.total_amount.amount),
            "reference": self.sales_invoice.reference,
        }

        # Delivery orders billed by this invoice. Templates decide whether to
        # print them — a header line of numbers, a detail block per albarán,
        # or nothing at all.
        delivery_orders = self._delivery_order_contexts()
        context["delivery_orders"] = delivery_orders
        context["delivery_order"] = delivery_orders[0] if delivery_orders else None
        context["has_delivery_orders"] = bool(delivery_orders)
        context["delivery_order_numbers"] = [
            do["number"] for do in delivery_orders if do["number"]
        ]

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
                "is_retention": tax.tax_type.behavior == "subtract",
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
