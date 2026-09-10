# sales/signals.py

from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from sales.models import DeliveryOrder, DeliveryOrderItem

# Delivery orders currently being deleted. A cascade-deleted line still has to
# give its stock back, but the compensating transaction must not reference the
# order that is about to disappear — the note carries the trace instead.
_deleting_delivery_orders: set[int] = set()


@receiver(pre_delete, sender=DeliveryOrder)
def delivery_order_deleting(sender, instance, **kwargs):
    _deleting_delivery_orders.add(instance.pk)


@receiver(post_delete, sender=DeliveryOrder)
def delivery_order_deleted(sender, instance, **kwargs):
    _deleting_delivery_orders.discard(instance.pk)


@receiver(pre_delete, sender=DeliveryOrderItem)
def return_stock_on_delivery_line_delete(sender, instance, **kwargs):
    """Deleting a delivery line — on its own or with its whole order — puts
    the goods back in the warehouse."""
    instance._return_inventory(
        keep_reference=instance.delivery_order_id not in _deleting_delivery_orders,
    )
