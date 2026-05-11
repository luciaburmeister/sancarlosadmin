from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords
from sales.models import Client
from company.models import CompanyOwnedModel, WorkingZone, BaseDocumentType, BaseDocument
from django.core.exceptions import ValidationError
from timestamps.models import Timestampable
from django.utils import timezone
from django.contrib.auth import get_user_model
from materials.models import Provider
User = get_user_model()


class AssetType(CompanyOwnedModel):
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
    current_working_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_assets",
        verbose_name=_("Current Working Zone")
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assets",
        verbose_name=_("Provider")
    )
    description = models.TextField(blank=True, verbose_name=_("Description"))
    assigned_to = models.ForeignKey(
        'company.Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_assets",
        verbose_name=_("Assigned To")
    )

    history = HistoricalRecords()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_working_zone = self.current_working_zone

    def save(self, *args, **kwargs):
        if self.pk:
            if self.current_working_zone != self._original_working_zone:
                self.create_transaction_on_change()
        super().save(*args, **kwargs)
        self._original_working_zone = self.current_working_zone

    def create_transaction_on_change(self):
        AssetTransaction.objects.create(
            asset=self,
            company=self.company,
            from_zone=self._original_working_zone,
            to_zone=self.current_working_zone,
            transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(),
            notes=_("Auto-generated movement from {from_z} to {to_z}").format(
                from_z=self._original_working_zone or 'None',
                to_z=self.current_working_zone or 'None'
            )
        )

    class Meta:
        verbose_name = _("Asset")
        verbose_name_plural = _("Assets")

    def __str__(self):
        return f"{self.name} ({self.asset_type.name})"


class AssetIdentifier(models.Model):
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


class TransactionTypeChoices(models.TextChoices):
    DELIVERY = "delivery", _("Delivery")
    RETURN = "return", _("Return")
    MOVEMENT = "movement", _("Movement")


class AssetTransaction(CompanyOwnedModel, Timestampable):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        related_name="transactions",
        verbose_name=_("Asset")
    )
    from_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions_from",
        verbose_name=_("From Zone")
    )
    to_zone = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transactions_to",
        verbose_name=_("To Zone")
    )
    transaction_type = models.CharField(
        max_length=10,
        choices=TransactionTypeChoices.choices,
        verbose_name=_("Transaction Type")
    )
    date = models.DateTimeField(default=timezone.now, verbose_name=_("Date"))
    data = models.JSONField(default=dict, blank=True, verbose_name=_("Flexible Data"))
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    firma = models.TextField(blank=True, null=True, verbose_name=_("Firma"))

    performed_by = models.ForeignKey(
    "company.Employee",
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name="performed_transactions"
    )


    class Meta:
        verbose_name = _("Asset transaction")
        verbose_name_plural = _("Asset transactions")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_transaction_type_display()} - {self.asset} ({self.from_zone} -> {self.to_zone})"


class AssetPlanning(CompanyOwnedModel, Timestampable):
    STATUS_CHOICES = [
        ('pending', _('Pending')),
        ('in_transit', _('In Transit')),
        ('delivered', _('Delivered')),
        ('cancelled', _('Cancelled')),
    ]
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="plannings",
        verbose_name=_("Asset")
    )
    origin = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="plannings_origin",
        verbose_name=_("From Zone")
    )
    destination = models.ForeignKey(
        WorkingZone,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="plannings_destination",
        verbose_name=_("To Zone")
    )
    assigned_to = models.ForeignKey(
        'company.Employee',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="asset_plannings",
        verbose_name=_("Assigned To")
    )
    scheduled_date = models.DateField(verbose_name=_("Scheduled Date"))
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        verbose_name=_("Status")
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Asset Planning")
        verbose_name_plural = _("Asset Plannings")
        ordering = ["-scheduled_date"]

    def __str__(self):
        return f"{self.asset.name} → {self.destination} ({self.scheduled_date})"


class AssetDocumentType(BaseDocumentType):
    class Meta:
        verbose_name = _("Asset Document Type")
        verbose_name_plural = _("Asset Document Types")


class AssetDocument(BaseDocument):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("Asset")
    )
    document_type = models.ForeignKey(
        AssetDocumentType,
        on_delete=models.PROTECT,
        related_name="documents",
        verbose_name=_("Document Type")
    )

    class Meta:
        verbose_name = _("Asset Document")
        verbose_name_plural = _("Asset Documents")

    def __str__(self):
        return f"{self.document_type.name} - {self.asset}"

    def save(self, *args, **kwargs):
        if not self.company_id and self.asset:
            self.company = self.asset.company
        super().save(*args, **kwargs)