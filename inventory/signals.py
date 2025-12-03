from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import InventoryMovement, InventoryItem


@receiver(post_save, sender=InventoryMovement)
def apply_inventory_movement(sender, instance, created, **kwargs):
    if not created:
        return

    item, _ = InventoryItem.objects.get_or_create(
        material=instance.material,
        location=instance.location,
    )

    item.quantity += instance.quantity_change
    item.save()
