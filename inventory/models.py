from django.db import models
from django.utils import timezone
from materials.models import Material
from timestamps.models import Timestampable
class InventoryItem(Timestampable):
    """
    Current stock level for a material at a given location.
    """

    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name="inventory_items",
    )

    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0)

    location = models.CharField(
        max_length=200,
        blank=True,
        help_text="Warehouse or storage location (optional)."
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("material", "location")
        verbose_name = "Inventory Item"
        verbose_name_plural = "Inventory Items"

    def __str__(self):
        loc = f" @ {self.location}" if self.location else ""
        return f"{self.material} — {self.quantity} {self.material.unit_type}{loc}"


class MovementType(models.TextChoices):
    IN = "IN", "Stock In"
    OUT = "OUT", "Stock Out"
    ADJUST = "ADJUST", "Adjustment"

class InventoryMovement(Timestampable):
    """
    A stock movement (entry, exit, or manual adjustment).
    """

    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        related_name="inventory_movements",
    )

    movement_type = models.CharField(
        max_length=10,
        choices=MovementType.choices,
    )

    quantity_change = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        help_text="Positive for stock-in, negative for stock-out."
    )

    timestamp = models.DateTimeField(default=timezone.now)

    user = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="User who performed the movement.",
    )

    location = models.CharField(
        max_length=200,
        blank=True,
        help_text="Location matching InventoryItem.",
    )

    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Inventory Movement"
        verbose_name_plural = "Inventory Movements"

    def __str__(self):
        return f"{self.timestamp} — {self.material} — {self.quantity_change:+f}"
