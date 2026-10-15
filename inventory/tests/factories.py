"""Shared inventory test fixtures: items with stock, accepted quote lines, deliveries."""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from inventory.choices import ItemType, MovementType
from inventory.models import InventoryItem, ItemTransaction
from invoices.services.delivery_order_service import DeliveryOrderService
from sales.models import Quote, QuoteInventoryItem, QuoteStatus
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService


def make_item(*, company, warehouse, code, name=None, stock=0, **kwargs):
    """Create a stockable InventoryItem, seeded with ``stock`` in its default warehouse."""
    item = InventoryItem.objects.create(
        code=code,
        name=code if name is None else name,
        company=company,
        item_type=kwargs.pop("item_type", ItemType.STOCKABLE),
        default_warehouse=kwargs.pop("default_warehouse", warehouse),
        **kwargs,
    )
    if stock:
        ItemTransaction.objects.create(
            item=item,
            warehouse=item.default_warehouse,
            movement_type=MovementType.IN,
            quantity_change=stock,
            note="Initial stock",
        )
    return item


def accepted_quote_line(*, company, client, item, quantity, project_file,
                        project_name="Q", status=QuoteStatus.ACCEPTED):
    """Create a one-line quote (accepted by default) for ``quantity`` of ``item``."""
    quote = Quote.objects.create(
        company=company,
        client=client,
        project_file=project_file,
        project_name=project_name,
        status=status,
    )
    return QuoteInventoryItem.objects.create(
        quote=quote,
        inventory_item=item,
        quantity=Decimal(quantity),
        unit_price=Decimal("100"),
    )


def deliver(*, quote_line, project_file, quantity=None):
    """Create a delivery order for a quote line and dispatch it (when the goods leave)."""
    quote = quote_line.quote
    do = DeliveryOrderService.create_delivery_order(
        project_file=project_file,
        company=quote.company,
        client=quote.client,
        items_data=[{
            "source_content_type_id": ContentType.objects.get_for_model(QuoteInventoryItem).id,
            "source_object_id": quote_line.pk,
            "description": quote_line.inventory_item.name,
            "quantity": quantity if quantity is not None else quote_line.quantity,
            "unit_price": quote_line.unit_price.amount,
        }],
    )
    DeliveryOrderFulfillmentService.dispatch(do)
    return do
