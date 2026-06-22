import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from sales.models import (
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteStatus,
)
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, AppliedTax
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method

logger = logging.getLogger(__name__)


class DeliveryOrderService:
    """
    Service for creating Delivery Orders from ProjectFile sources and
    for creating SalesInvoices from Delivery Orders.

    Supports two invoicing strategies:
    1. Direct: ProjectFile → SalesInvoice (existing flow, unchanged)
    2. Via Delivery Order: ProjectFile → DeliveryOrder → SalesInvoice

    Items can be sourced from:
    - Configurations (from accepted quotes)
    - Direct Configurations (shop/work-order flow, no quote)
    - QuoteInventoryItems (inventory items attached to quotes)
    """

    @classmethod
    def get_deliverable_items(cls, project_file):
        """
        Returns all items eligible for delivery from a ProjectFile.
        Subtracts already-delivered quantities from existing delivery orders.

        Returns a list of dicts, each with:
        - source_content_type_id, source_object_id: GenericFK identifiers
        - source_type: str label ('configuration', 'direct_configuration', 'inventory_item')
        - description, reference: display fields
        - total_quantity: total available quantity
        - delivered_quantity: quantity already in non-cancelled delivery orders
        - remaining_quantity: total_quantity - delivered_quantity
        - unit_price: price per unit (Decimal)
        - discount_percentage: discount (Decimal)
        - source_object: the actual Django model instance
        """
        from configurator.models import Configuration, ConfigurationStatus
        from sales.models import QuoteInventoryItem

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        # Gather all already-delivered quantities by (content_type_id, object_id)
        delivered_map = cls._get_delivered_quantities(project_file)

        items = []

        # 1. Configurations from accepted quotes
        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        )

        for quote in accepted_quotes:
            # Ratio of discounted to original subtotal
            quote_sub = quote.subtotal_price.amount if quote.subtotal_price else Decimal("0")
            discounted_sub = quote.discounted_subtotal.amount if quote.discounted_subtotal else quote_sub

            for config in quote.configurations.all():
                key = (config_ct.id, config.id)
                total_qty = config.units if config.units and config.units > 0 else Decimal("1")
                delivered_qty = delivered_map.get(key, Decimal("0"))
                remaining = total_qty - delivered_qty
                if remaining <= 0:
                    continue

                # Config's share of the discounted subtotal
                config_sub = config.subtotal_price.amount
                if quote_sub > 0:
                    actual_sub = (discounted_sub * config_sub / quote_sub).quantize(Decimal("0.01"))
                else:
                    actual_sub = config_sub
                up = (actual_sub / total_qty).quantize(Decimal("0.01")) if total_qty > 0 else actual_sub

                items.append({
                    'source_content_type_id': config_ct.id,
                    'source_object_id': config.id,
                    'source_type': 'configuration',
                    'description': f"{config.name} (Q#{quote.id})",
                    'reference': f"CFG-{config.id}",
                    'total_quantity': total_qty,
                    'delivered_quantity': delivered_qty,
                    'remaining_quantity': remaining,
                    'unit_price': up,
                    'discount_percentage': Decimal("0.00"),
                    'source_object': config,
                    'quote_id': quote.id,
                })

            # Inventory items attached to the quote
            for inv_item in quote.inventory_items.select_related("inventory_item").all():
                key = (inv_item_ct.id, inv_item.id)
                total_qty = inv_item.quantity
                delivered_qty = delivered_map.get(key, Decimal("0"))
                remaining = total_qty - delivered_qty
                if remaining <= 0:
                    continue

                inv_name = (
                    inv_item.inventory_item.name
                    if inv_item.inventory_item_id
                    else f"Inventory Item #{inv_item.pk}"
                )

                items.append({
                    'source_content_type_id': inv_item_ct.id,
                    'source_object_id': inv_item.id,
                    'source_type': 'inventory_item',
                    'description': f"{inv_name} (Q#{quote.id})",
                    'reference': inv_item.inventory_item.code if inv_item.inventory_item_id else "",
                    'total_quantity': total_qty,
                    'delivered_quantity': delivered_qty,
                    'remaining_quantity': remaining,
                    'unit_price': inv_item.unit_price.amount if inv_item.unit_price else Decimal("0"),
                    'discount_percentage': inv_item.discount if inv_item.discount else Decimal("0"),
                    'source_object': inv_item,
                    'quote_id': quote.id,
                })

        # 2. Direct configurations (shop/work-order flow, NOT via a quote)
        direct_configs = Configuration.objects.filter(
            project_file=project_file,
            quote__isnull=True,
            status__in=[
                ConfigurationStatus.SHOP,
                ConfigurationStatus.COMPLETED,
                ConfigurationStatus.CONVERTED,
                ConfigurationStatus.PRODUCTION_DONE,
            ],
        )
        for config in direct_configs:
            key = (config_ct.id, config.id)
            total_qty = config.units if config.units and config.units > 0 else Decimal("1")
            delivered_qty = delivered_map.get(key, Decimal("0"))
            remaining = total_qty - delivered_qty
            if remaining <= 0:
                continue

            sub = config.subtotal_price.amount
            up = (sub / total_qty).quantize(Decimal("0.01")) if total_qty > 0 else sub

            items.append({
                'source_content_type_id': config_ct.id,
                'source_object_id': config.id,
                'source_type': 'direct_configuration',
                'description': config.name,
                'reference': f"CFG-{config.id}",
                'total_quantity': total_qty,
                'delivered_quantity': delivered_qty,
                'remaining_quantity': remaining,
                'unit_price': up,
                'discount_percentage': Decimal("0.00"),
                'source_object': config,
                'quote_id': None,
            })

        return items

    @classmethod
    def _get_delivered_quantities(cls, project_file):
        """
        Returns a dict of {(content_type_id, object_id): total_delivered_quantity}
        for all non-cancelled delivery orders linked to this project file.
        """
        delivered = {}
        existing_dos = DeliveryOrder.objects.filter(
            project_file=project_file,
        ).exclude(status=DeliveryOrderStatus.CANCELLED)

        for do in existing_dos:
            for item in do.items.filter(source_content_type__isnull=False):
                key = (item.source_content_type_id, item.source_object_id)
                delivered[key] = delivered.get(key, Decimal("0")) + item.quantity

        return delivered

    @classmethod
    @transaction.atomic
    def create_delivery_order(
        cls,
        project_file,
        company,
        client,
        items_data,
        date=None,
        notes="",
    ):
        """
        Creates a DeliveryOrder with items, validating quantities against
        remaining deliverable amounts.

        Args:
            project_file: The ProjectFile
            company: Company
            client: Client
            items_data: List of dicts with:
                - source_content_type_id: ContentType ID
                - source_object_id: Source object PK
                - description: str
                - quantity: Decimal
                - unit_price: Decimal
                - discount_percentage: Decimal (optional)
                - reference: str (optional)
            date: Date for the delivery order (defaults to today)
            notes: str

        Returns:
            DeliveryOrder instance
        """
        from invoices.services.sales_invoice_service import SalesInvoiceService

        if not items_data:
            raise ValidationError(_("At least one item is required."))

        next_do = SalesInvoiceService._generate_next_delivery_order_data(company)

        delivery_order = DeliveryOrder.objects.create(
            company=company,
            number=next_do['number'],
            incremental_id=next_do['incremental_id'],
            date=date or timezone.now().date(),
            client=client,
            project_file=project_file,
            notes=notes,
            status=DeliveryOrderStatus.DRAFT,
        )

        for item_data in items_data:
            ct_id = item_data.get('source_content_type_id')
            obj_id = item_data.get('source_object_id')

            DeliveryOrderItem.objects.create(
                delivery_order=delivery_order,
                description=item_data['description'],
                quantity=item_data.get('quantity', Decimal("1")),
                unit_price=item_data.get('unit_price', Decimal("0")),
                discount_percentage=item_data.get('discount_percentage', Decimal("0")),
                source_content_type_id=ct_id,
                source_object_id=obj_id,
                reference=item_data.get('reference', ''),
            )

        delivery_order.recalculate_subtotal()

        logger.info(
            "Created DeliveryOrder %s from ProjectFile %s. Items: %d, Subtotal: %s",
            delivery_order.number,
            project_file.reference,
            len(items_data),
            delivery_order.subtotal_amount,
        )

        return delivery_order

    @classmethod
    def get_invoiceable_delivery_orders(cls, project_file):
        """
        Returns delivery orders linked to a ProjectFile that have items
        not yet associated with an invoice (sales_invoice is null).
        """
        return DeliveryOrder.objects.filter(
            project_file=project_file,
            sales_invoice__isnull=True,
        ).exclude(
            status=DeliveryOrderStatus.CANCELLED,
        ).order_by('date', 'number')

    @classmethod
    @transaction.atomic
    def create_invoice_from_delivery_orders(
        cls,
        delivery_orders,
        company,
        client,
        date_issued,
        due_date=None,
        notes="",
        payment_method=None,
    ):
        """
        Creates a SalesInvoice from one or more DeliveryOrders.

        Each delivery order item becomes an invoice line item.
        Taxes are copied from the source objects' AppliedTax records.

        Args:
            delivery_orders: queryset or list of DeliveryOrder instances
            company: Company
            client: Client
            date_issued: date
            due_date: date (optional)
            notes: str
            payment_method: str (optional)

        Returns:
            SalesInvoice instance
        """
        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        resolved_payment_method = resolve_sales_invoice_payment_method(
            client=client,
            explicit=payment_method,
        )

        project_file = None
        for do in delivery_orders:
            if do.project_file_id:
                project_file = do.project_file
                break

        invoice = SalesInvoice.objects.create(
            company=company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            date_issued=date_issued,
            due_date=due_date,
            client=client,
            project_file=project_file,
            notes=notes,
            payment_method=resolved_payment_method,
        )

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        subtotal = Decimal("0.00")
        tax_agg = {}  # (tax_type_id, behavior, percentage) → aggregated amounts

        for do in delivery_orders:
            for item in do.items.all():
                source_id = cls._build_source_id(item)

                inv_item = SalesInvoiceItem.objects.create(
                    invoice=invoice,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    discount_percentage=item.discount_percentage,
                    source_id=source_id,
                    reference=item.reference,
                )
                line_total = inv_item.line_total.amount
                subtotal += line_total

                # Resolve taxes from the source object
                source_taxes = []
                if item.source_content_type_id == config_ct.id and item.source_object_id:
                    source_taxes = list(
                        AppliedTax.objects.filter(
                            content_type=config_ct,
                            object_id=item.source_object_id,
                        ).select_related("tax_type")
                    )
                elif item.source_content_type_id == inv_item_ct.id and item.source_object_id:
                    source_taxes = list(
                        AppliedTax.objects.filter(
                            content_type=inv_item_ct,
                            object_id=item.source_object_id,
                        ).select_related("tax_type")
                    )

                for tax in source_taxes:
                    if tax.selected_percentage <= 0:
                        continue
                    key = (tax.tax_type_id, tax.tax_type.behavior, tax.selected_percentage)
                    rate = tax.selected_percentage / Decimal("100")
                    calc = (line_total * rate).quantize(Decimal("0.01"))

                    if key not in tax_agg:
                        tax_agg[key] = {
                            "tax_type": tax.tax_type,
                            "selected_percentage": tax.selected_percentage,
                            "base_amount": Decimal("0.00"),
                            "calculated_amount": Decimal("0.00"),
                        }
                    tax_agg[key]["base_amount"] += line_total
                    tax_agg[key]["calculated_amount"] += calc

            # Link the delivery order to the invoice
            do.sales_invoice = invoice
            do.save(update_fields=["sales_invoice"])

        # Create aggregated AppliedTax records on the invoice
        ct = ContentType.objects.get_for_model(invoice)
        computed_total_tax = Decimal("0.00")

        for order, entry in enumerate(tax_agg.values()):
            computed_total_tax += entry["calculated_amount"]
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=entry["tax_type"],
                selected_percentage=entry["selected_percentage"],
                base_amount=entry["base_amount"],
                calculated_amount=entry["calculated_amount"],
                order=order,
            )

        invoice.subtotal_amount = subtotal
        invoice.tax_amount = computed_total_tax
        invoice.total_amount = subtotal + computed_total_tax
        invoice.save(update_fields=['subtotal_amount', 'tax_amount', 'total_amount'])

        logger.info(
            "Created SalesInvoice #%s from %d DeliveryOrders. "
            "Amount: %s (subtotal: %s, tax: %s)",
            invoice.id,
            len(list(delivery_orders)),
            invoice.total_amount,
            invoice.subtotal_amount,
            invoice.tax_amount,
        )

        return invoice

    @classmethod
    def _build_source_id(cls, delivery_order_item):
        """
        Builds a source_id string for a SalesInvoiceItem from a DeliveryOrderItem's
        GenericFK, matching the format used by the existing ProjectFileInvoiceService.
        """
        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        if not delivery_order_item.source_content_type_id:
            return ""

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        if delivery_order_item.source_content_type_id == config_ct.id:
            return f"config_{delivery_order_item.source_object_id}"
        elif delivery_order_item.source_content_type_id == inv_item_ct.id:
            return f"inv_item_{delivery_order_item.source_object_id}"

        return f"source_{delivery_order_item.source_content_type_id}_{delivery_order_item.source_object_id}"

    @classmethod
    def get_delivery_summary(cls, project_file):
        """
        Returns a dict with the delivery status for a ProjectFile:
        - deliverable_items: list of items eligible for delivery
        - delivery_orders: list of existing delivery orders
        - total_deliverable_value: sum of all deliverable item values
        - total_delivered_value: sum of all delivered item values
        """
        items = cls.get_deliverable_items(project_file)

        existing_dos = DeliveryOrder.objects.filter(
            project_file=project_file,
        ).exclude(
            status=DeliveryOrderStatus.CANCELLED,
        ).order_by('-date')

        total_deliverable = sum(
            (i['unit_price'] * i['total_quantity'] * (1 - i['discount_percentage'] / 100) for i in items),
            Decimal("0.00"),
        )
        total_delivered = sum(
            (do.subtotal_amount.amount for do in existing_dos if do.subtotal_amount),
            Decimal("0.00"),
        )

        return {
            'deliverable_items': items,
            'delivery_orders': existing_dos,
            'total_deliverable_value': total_deliverable,
            'total_delivered_value': total_delivered,
        }
