"""Signals for the purchasing app."""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="purchasing.PurchaseOrder")
def purchase_order_created_notify(sender, instance, created, **kwargs):
    """Notify the company's configured recipients when a Purchase Order is created."""
    if not created or not instance.company_id:
        return

    from purchasing.tasks import send_purchase_order_created_notification

    send_purchase_order_created_notification.delay(instance.pk)
