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

class MovementType(models.TextChoices):
    IN = "IN", _("Stock In")
    OUT = "OUT", _("Stock Out")
    ADJUST = "ADJUST", _("Adjustment")
    TRANSFER = "TRANSFER", _("Transfer")
