from django.db import models
from django.utils.translation import gettext_lazy as _

class LocationType(models.TextChoices):
    WAREHOUSE = "WAREHOUSE", _("Warehouse")
    BIN = "BIN", _("Bin/Slot")
    OTHER = "OTHER", _("Other")

class ItemType(models.TextChoices):
    STOCKABLE = "STOCKABLE", _("Stockable Item")
    SERVICE = "SERVICE", _("Service")
    FINISHED_PRODUCT = "FINISHED_PRODUCT", _("Finished Product")
    OWN_PRODUCTION = "OWN_PRODUCTION", _("Own Production")

class OnlineSaleMode(models.TextChoices):
    BUY = "buy", _("Buy")
    QUOTE = "quote", _("Request a quote")
    BOTH = "both", _("Buy or request a quote")

class MovementType(models.TextChoices):
    IN = "IN", _("Stock In")
    OUT = "OUT", _("Stock Out")
    ADJUST = "ADJUST", _("Adjustment")
    TRANSFER = "TRANSFER", _("Transfer")

class ItemSchemaKind(models.TextChoices):
    PRODUCT = "product", _("Product schema")
    BLOCK = "block", _("Reusable block")
