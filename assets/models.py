from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords
from sales.models import Client
from company.models import CompanyOwnedModel
from django.core.exceptions import ValidationError

class AssetType(CompanyOwnedModel):
    """
    Defines the type of asset (e.g., Car, Truck, Machinery).
    Can eventually hold a JSON schema for validation.
    """
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    schema = models.JSONField(
        default=dict, 
        blank=True, 
        help_text=_("JSON Schema to validate asset data (optional)")
    )

    class Meta:
        verbose_name = _("Asset type")
        verbose_name_plural = _("Asset types")

    def __str__(self):
        return self.name

class Asset(CompanyOwnedModel):
    """
    An individual asset instance.
    """
    asset_type = models.ForeignKey(
        AssetType, 
        on_delete=models.PROTECT, 
        related_name="assets",
        verbose_name=_("Asset Type")
    )
    client = models.ForeignKey(
        Client,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("Owner/Client")
    )
    name = models.CharField(max_length=200, verbose_name=_("Name/Label"))
    data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_("Flexible Data"),
        help_text=_("Schema-less data for this specific asset type.")
    )
    
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset")
        verbose_name_plural = _("Assets")

    def __str__(self):
        return f"{self.name} ({self.asset_type.name})"

class AssetIdentifier(models.Model):
    """
    Identifiers for search (VIN, Plate, Serial Number).
    One asset can have multiple identifiers.
    """
    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        related_name="identifiers",
        verbose_name=_("Asset")
    )
    label = models.CharField(
        max_length=50, 
        verbose_name=_("Label"),
        help_text=_("e.g., VIN, Plate Number, SN")
    )
    value = models.CharField(
        max_length=100, 
        verbose_name=_("Value"),
        db_index=True
    )

    class Meta:
        verbose_name = _("Asset identifier")
        verbose_name_plural = _("Asset identifiers")
        unique_together = ("label", "value")

    def __str__(self):
        return f"{self.label}: {self.value}"

    def clean(self):
        super().clean()
        if self.asset and self.asset.company:
            labels = self.asset.company.asset_identifier_labels
            if labels and self.label not in labels:
                raise ValidationError({
                    'label': _("This label is not allowed for this company. Allowed: %(labels)s") % {'labels': ", ".join(labels)}
                })
