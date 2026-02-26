from django.db import models 
from django.utils import timezone 
from django.utils.translation import gettext_lazy as _ 
from django.contrib.contenttypes.fields import GenericForeignKey 
from django.contrib.contenttypes.models import ContentType 
from timestamps.models import Timestampable
import re
from django.db import transaction
from django.core.exceptions import ValidationError

from company.models import CompanyOwnedModel 
from materials.models import Provider
from .choices import LocationType, ItemType, MovementType


class InventoryStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    REQUESTED = "REQUESTED", _("Requested")
    RESERVED = "RESERVED", _("Reserved")


class InventoryCategory(Timestampable):
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        verbose_name = "Inventory Category"
        verbose_name_plural = "Inventory Categories"

    def __str__(self):
        return self.name


class Warehouse(Timestampable, CompanyOwnedModel):
    code = models.CharField(max_length=30, db_index=True)
    name = models.CharField(max_length=120)

    country = models.CharField(max_length=120, blank=True)
    city = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=30, blank=True)
    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Warehouse"
        verbose_name_plural = "Warehouses"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"],
                name="uniq_warehouse_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "code"], name="idx_wh_company_code"),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class StockLocation(Timestampable):
    """
    Structured locations inside a warehouse or standalone.
    - Keeps optional FK to Warehouse for backwards compatibility (warehouse field).
    - Also accepts a generic link (content_type/object_id) to any other module's object.
    - If `source_object` is set and `name` or `code` are blank, they will be
      populated from the source_object (if that object exposes .name and/or .code).
    """

    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="locations",
        null=True,
        blank=True,
        help_text="(Legacy) parent warehouse; optional if you use a generic source.",
    )

    # NEW: explicit type so you can pick what kind of loc this is
    location_type = models.CharField(max_length=20, choices=LocationType.choices, default=LocationType.BIN, db_index=True)

    # Allow linking to an arbitrary source model (Material, Warehouse master, etc.)
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="stock_locations",
        null=True,
        blank=True,
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_object = GenericForeignKey("content_type", "object_id")

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "code"],
                name="uniq_stock_location_warehouse_code",
            )
        ]
        verbose_name = "Stock Location"
        verbose_name_plural = "Stock Locations"

    def __str__(self):
        # Prefer the explicit code/name; if linked to a source and that has a display, use it
        if self.source_object:
            # source_object may be any model — use its __str__ if available
            return f"{self.source_object} ({self.code or self.name})"
        return f"{self.warehouse or ''} / {self.code or self.name}"

    def save(self, *args, **kwargs):
        """
        If this StockLocation references a source_object (Generic FK), and the
        current name/code are blank, attempt to populate them from the source.
        This is the 'auto-register the code etc.' behavior you requested.
        """
        src = self.source_object
        # Only attempt when a source is present
        if src is not None:
            # Try common attribute names; be forgiving.
            try:
                # if name blank, try common attributes
                if (not self.name or self.name.strip() == ""):
                    for attr in ("name", "title", "display_name"):
                        if hasattr(src, attr):
                            v = getattr(src, attr)
                            if v:
                                self.name = str(v)
                                break
                # if code blank, try common attributes
                if (not self.code or self.code.strip() == ""):
                    for attr in ("code", "sku", "reference", "slug"):
                        if hasattr(src, attr):
                            v = getattr(src, attr)
                            if v:
                                self.code = str(v)
                                break
            except Exception:
                # never raise on save auto-fill — fail silently so admin flow isn't blocked
                pass

        super().save(*args, **kwargs)


def _item_prefix(item_type: str) -> str:
    return {
        ItemType.STOCKABLE: "STK",
        ItemType.SERVICE: "SRV",
        ItemType.FINISHED_PRODUCT: "FP",
    }.get(item_type, "ITM")


class InventoryItem(Timestampable, CompanyOwnedModel):
    code = models.CharField(max_length=20, blank=True, db_index=True)
    name = models.CharField(max_length=200, db_index=True, null=True, blank=True)
    description = models.TextField(blank=True)

    item_type = models.CharField(
        max_length=30,
        choices=ItemType.choices,
        default=ItemType.STOCKABLE,
        db_index=True,
    )

    default_warehouse = models.ForeignKey(
        "inventory.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="items",
        help_text="Default warehouse for stocked/finished items. Services do not use warehouses.",
    )

    preferred_supplier = models.ForeignKey(
        Provider,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplied_items",
        help_text="Preferred supplier for procurement.",
    )

    image = models.ImageField(upload_to="inventory/items/images/", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"],
                name="uniq_item_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "item_type"], name="idx_item_company_type"),
        ]
        verbose_name = "Item"
        verbose_name_plural = "Items"

    def __str__(self):
        return f"{self.code or '—'} — {self.name}"

    def clean(self):
        super().clean()
        # Services must not have a warehouse
        if self.item_type == ItemType.SERVICE and self.default_warehouse_id:
            raise ValidationError({"default_warehouse": _("Services cannot have a warehouse.")})
        # Stockable/Finished should have a warehouse (ERP logic)
        if self.item_type in (ItemType.STOCKABLE, ItemType.FINISHED_PRODUCT) and not self.default_warehouse_id:
            raise ValidationError({"default_warehouse": _("This item type requires a warehouse.")})

    @transaction.atomic
    def save(self, *args, **kwargs):
        # keep data consistent even outside admin
        if self.item_type == ItemType.SERVICE:
            self.default_warehouse = None

        # Generate code if blank
        if not self.code:
            prefix = _item_prefix(self.item_type)
            # Find max numeric suffix for same company + item_type + prefix
            # Pattern: PREFIX-00001
            qs = InventoryItem.objects.select_for_update().filter(
                company=self.company,
                item_type=self.item_type,
                code__startswith=f"{prefix}-",
            )

            # Extract integer suffix in DB-ish way (simple + safe)
            # We'll do a Python fallback scan if needed.
            max_num = 0
            for c in qs.values_list("code", flat=True):
                m = re.match(rf"^{re.escape(prefix)}-(\d{{5}})$", c or "")
                if m:
                    max_num = max(max_num, int(m.group(1)))

            next_num = max_num + 1
            self.code = f"{prefix}-{next_num:05d}"

        super().save(*args, **kwargs)

    @property
    def stock_on_hand(self) -> int:
        """
        ERP-style: calculated from movements (no stored quantity).
        """
        return (
            self.movements.aggregate(total=models.Sum("quantity_change")).get("total") or 0
        )

class MovementType(models.TextChoices):
    IN = "IN", _("Stock In")
    OUT = "OUT", _("Stock Out")
    ADJUST = "ADJUST", _("Adjustment")
    TRANSFER = "TRANSFER", _("Transfer")  


class InventoryMovement(Timestampable):
    """
    A stock movement (entry, exit, adjustment, transfer).
    quantity_change: positive = stock in, negative = stock out.
    """

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.PROTECT,
        related_name="inventory_movements",
        null=True,
        blank=True,
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    source_object = GenericForeignKey("content_type", "object_id")

    movement_type = models.CharField(max_length=10, choices=MovementType.choices)

    quantity_change = models.IntegerField(
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

    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        related_name="movements",
    )

    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        related_name="movements",
    )


    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["timestamp"], name="idx_invmov_timestamp"),
            models.Index(fields=["movement_type"], name="idx_invmov_type"),
            models.Index(fields=["content_type", "object_id"], name="idx_invmov_source"),
            models.Index(fields=["item"], name="idx_invmov_item"),
            models.Index(fields=["warehouse"], name="idx_invmov_wh"),
        ]

        verbose_name = "Inventory Movement"
        verbose_name_plural = "Inventory Movements"

    def __str__(self):
        obj = self.source_object
        obj_label = str(obj) if obj else "—"
        return f"{self.timestamp:%Y-%m-%d %H:%M} — {obj_label} — {self.quantity_change:+d}"
    
    def clean(self):
        super().clean()
        if self.item and self.item.item_type == ItemType.SERVICE:
            raise ValidationError({"item": _("You cannot create stock movements for services.")})

        if self.item and self.warehouse and self.item.company_id != self.warehouse.company_id:
            raise ValidationError({"warehouse": _("Warehouse must belong to the same company as the item.")})



class ItemAttachment(Timestampable, CompanyOwnedModel):
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="inventory/items/attachments/")
    label = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name = "Item Attachment"
        verbose_name_plural = "Item Attachments"

    def __str__(self):
        return self.label or str(self.file)
