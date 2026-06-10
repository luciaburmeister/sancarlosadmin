from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django_countries.fields import CountryField
from djmoney.models.fields import MoneyField
from timestamps.models import Timestampable
from django.db import transaction
from django.core.exceptions import ValidationError

from company.models import CompanyOwnedModel
from materials.models import Provider
from .choices import LocationType, ItemType, MovementType
from configurator.utils.codegen import next_scoped_code


class InventoryStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    REQUESTED = "REQUESTED", _("Requested")
    RESERVED = "RESERVED", _("Reserved")


class InventoryCategory(Timestampable, CompanyOwnedModel):
    name = models.CharField(_("Name"), max_length=120)

    class Meta:
        verbose_name = _("Inventory Category")
        verbose_name_plural = _("Inventory Categories")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                name="uniq_inventory_category_company_name",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "name"], name="idx_invcat_company_name"),
        ]

    def __str__(self):
        return self.name


class Warehouse(Timestampable, CompanyOwnedModel):
    code = models.CharField(_("Code"), max_length=30, db_index=True)
    name = models.CharField(_("Name"), max_length=120)
    country = CountryField(_("Country"), blank=True)
    city = models.CharField(_("City"), max_length=120, blank=True)
    postal_code = models.CharField(_("Postal Code"), max_length=30, blank=True)
    address_line1 = models.CharField(_("Address Line 1"), max_length=200, blank=True)
    address_line2 = models.CharField(_("Address Line 2"), max_length=200, blank=True)
    is_active = models.BooleanField(_("Active"), default=True)

    class Meta:
        verbose_name = _("Warehouse")
        verbose_name_plural = _("Warehouses")
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
        return f"{self.code} | {self.name}"


class StockLocation(Timestampable):
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        on_delete=models.PROTECT,
        related_name="locations",
        null=True,
        blank=True,
    )
    location_type = models.CharField(
        _("Location Type"),
        max_length=20,
        choices=LocationType.choices,
        default=LocationType.BIN,
        db_index=True,
    )
    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("Content Type"),
        on_delete=models.PROTECT,
        related_name="inventory_stock_locations",
        null=True,
        blank=True,
    )
    object_id = models.PositiveBigIntegerField(_("Object ID"), null=True, blank=True)
    source_object = GenericForeignKey("content_type", "object_id")

    name = models.CharField(_("Name"), max_length=120)
    code = models.CharField(_("Code"), max_length=40, blank=True)
    is_active = models.BooleanField(_("Active"), default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "code"],
                name="uniq_stock_location_warehouse_code",
            )
        ]
        verbose_name = _("Stock Location")
        verbose_name_plural = _("Stock Locations")

    def __str__(self):
        return f"{self.source_object} ({self.code or self.name})" if self.source_object else (self.code or self.name)

    def clean(self):
        super().clean()
        if bool(self.content_type_id) ^ bool(self.object_id):
            raise ValidationError(
                {"content_type": _("Select both Content Type and Object ID, or leave both empty.")}
            )


def _item_prefix(item_type: str) -> str:
    return {
        ItemType.STOCKABLE: "STK",
        ItemType.SERVICE: "SRV",
        ItemType.FINISHED_PRODUCT: "FP",
    }.get(item_type, "ITM")


class InventoryItem(Timestampable, CompanyOwnedModel):
    code = models.CharField(_("Code"), max_length=20, blank=True, db_index=True)
    name = models.CharField(_("Name"), max_length=200, db_index=True, null=True, blank=True)
    description = models.TextField(_("Description"), blank=True)
    item_type = models.CharField(
        _("Item Type"),
        max_length=30,
        choices=ItemType.choices,
        default=ItemType.STOCKABLE,
        db_index=True,
    )
    default_warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="items",
    )
    preferred_supplier = models.ForeignKey(
        Provider,
        verbose_name=_("Preferred Supplier"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplied_items",
    )
    invoice = models.ForeignKey(
        "invoices.Invoice",
        verbose_name=_("Invoice"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_items",
    )
    product_content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("Product Content Type"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_items",
    )
    product_object_id = models.PositiveBigIntegerField(_("Product Object ID"), null=True, blank=True)
    product_object = GenericForeignKey("product_content_type", "product_object_id")

    image = models.ImageField(_("Image"), upload_to="inventory/items/images/", null=True, blank=True)

    selling_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Selling Price"),
        null=True,
        blank=True,
    )

    min_stock = models.PositiveIntegerField(
        _("Minimum Stock"),
        null=True,
        blank=True,
        help_text=_("Alert threshold: an order reminder will be shown when stock falls below this level."),
    )

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
        verbose_name = _("Item")
        verbose_name_plural = _("Items")

    def __str__(self):
        return f"{self.code} | {self.name}"

    def clean(self):
        super().clean()
        if self.item_type == ItemType.SERVICE and self.default_warehouse_id:
            raise ValidationError({"default_warehouse": _("Services cannot have a warehouse.")})
        if self.item_type in (ItemType.STOCKABLE, ItemType.FINISHED_PRODUCT) and not self.default_warehouse_id:
            raise ValidationError({"default_warehouse": _("This item type requires a warehouse.")})

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.item_type == ItemType.SERVICE:
            self.default_warehouse = None

        if not self.code:
            prefix = _item_prefix(self.item_type)
            self.code = next_scoped_code(
                model=InventoryItem,
                scope_filter={"company": self.company, "item_type": self.item_type},
                prefix=prefix,
                width=5,
                separator="-",
            )

        super().save(*args, **kwargs)

    @property
    def stock_on_hand(self) -> int:
        return self.transactions.aggregate(total=models.Sum("quantity_change")).get("total") or 0

    @property
    def is_low_stock(self) -> bool:
        """True when min_stock is set and current stock is at or below it."""
        return self.min_stock is not None and self.stock_on_hand <= self.min_stock

    def get_taxes(self):
        """Return AppliedTax records attached to this item via GenericFK."""
        from invoices.models import AppliedTax
        ct = ContentType.objects.get_for_model(self)
        return AppliedTax.objects.filter(
            content_type=ct, object_id=self.pk
        ).select_related("tax_type").order_by("order")


class ItemProviderInfo(Timestampable):
    """
    Stores a provider-specific reference / SKU for an inventory item.
    Each provider can assign its own unique identifier to the item.
    """

    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.CASCADE,
        related_name="provider_info",
    )
    provider = models.ForeignKey(
        Provider,
        verbose_name=_("Provider"),
        on_delete=models.CASCADE,
        related_name="item_info",
    )
    provider_ref = models.CharField(
        _("Provider Reference"),
        max_length=100,
        help_text=_("The provider's own code / SKU for this item."),
    )

    class Meta:
        verbose_name = _("Item Provider Info")
        verbose_name_plural = _("Item Provider Info")
        constraints = [
            models.UniqueConstraint(
                fields=["item", "provider_ref"],
                name="uniq_item_provider_ref",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_ref"],
                name="uniq_provider_item_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["provider_ref"], name="idx_item_provider_ref"),
        ]

    def __str__(self):
        return f"{self.provider.name} → {self.provider_ref}"


class ItemTransaction(Timestampable):
    """
    Tracks every state change / physical movement an inventory item goes through.

    quantity_change: positive = stock in, negative = stock out.
    Use `reference` to link back to the originating document
    (purchase order, production ticket, transfer order, etc.) via a GenericForeignKey.
    """

    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        verbose_name=_("Stock Location"),
        on_delete=models.SET_NULL,
        related_name="transactions",
        null=True,
        blank=True,
        help_text=_("Specific bin / rack / shelf inside the warehouse (optional)."),
    )
    movement_type = models.CharField(
        _("Movement Type"),
        max_length=10,
        choices=MovementType.choices,
        db_index=True,
    )
    quantity_change = models.IntegerField(
        _("Quantity Change"),
        help_text=_("Positive for stock-in, negative for stock-out."),
    )
    # ── Generic FK so any document can be a source ──────────────────────────
    reference_content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("Reference Type"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="item_transactions",
        help_text=_("Type of the originating document (e.g. PurchaseOrder, ProductionTicket)."),
    )
    reference_object_id = models.PositiveBigIntegerField(
        _("Reference ID"),
        null=True,
        blank=True,
        help_text=_("PK of the originating document."),
    )
    reference_object = GenericForeignKey("reference_content_type", "reference_object_id")

    user = models.ForeignKey(
        "auth.User",
        verbose_name=_("User"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("User who recorded the transaction."),
    )
    note = models.TextField(_("Note"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"], name="idx_itemtx_created_at"),
            models.Index(fields=["movement_type"], name="idx_itemtx_type"),
            models.Index(fields=["item"], name="idx_itemtx_item"),
            models.Index(fields=["warehouse"], name="idx_itemtx_wh"),
            models.Index(
                fields=["reference_content_type", "reference_object_id"],
                name="idx_itemtx_ref",
            ),
        ]
        verbose_name = _("Item Transaction")
        verbose_name_plural = _("Item Transactions")

    def __str__(self):
        return (
            f"{self.created_at:%Y-%m-%d %H:%M} — {self.item} — "
            f"{self.get_movement_type_display()} ({self.quantity_change:+d})"
        )

    def clean(self):
        super().clean()
        if bool(self.reference_content_type_id) ^ bool(self.reference_object_id):
            raise ValidationError(
                {
                    "reference_content_type": _(
                        "Provide both Reference Type and Reference ID, or leave both empty."
                    )
                }
            )


# ── Keep the original InventoryMovement for backwards compatibility ──────────

class InventoryMovement(Timestampable):
    """Legacy stock movement model — superseded by ItemTransaction.
    Kept to avoid breaking existing migrations / data."""

    movement_type = models.CharField(
        _("Movement Type"),
        max_length=10,
        choices=MovementType.choices,
        db_index=True,
    )
    quantity_change = models.IntegerField(
        _("Quantity Change"),
        help_text=_("Positive for stock-in, negative for stock-out."),
    )
    user = models.ForeignKey(
        "auth.User",
        verbose_name=_("User"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("User who performed the movement."),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.PROTECT,
        related_name="movements",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        on_delete=models.PROTECT,
        related_name="movements",
    )
    note = models.TextField(_("Note"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"], name="idx_invmov_created_at"),
            models.Index(fields=["movement_type"], name="idx_invmov_type"),
            models.Index(fields=["item"], name="idx_invmov_item"),
            models.Index(fields=["warehouse"], name="idx_invmov_wh"),
        ]
        verbose_name = _("Inventory Movement (legacy)")
        verbose_name_plural = _("Inventory Movements (legacy)")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} — {self.item} — {self.quantity_change:+d}"