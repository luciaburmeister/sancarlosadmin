from django.db import models, transaction
from decimal import Decimal
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from djmoney.models.fields import MoneyField
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel
from materials.models import Provider, UnitChoices
from inventory.models import InventoryItem, Warehouse, ItemTransaction
from inventory.choices import ItemType, MovementType
from configurator.utils.codegen import next_scoped_code
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator

def po_file_path(instance, filename):
    """Path: purchasing/pos/company_{id}/{filename}"""
    return f"purchasing/pos/company_{instance.company_id}/{filename}"

class PurchaseOrderType(models.TextChoices):
    PROJECT = "PROJECT", _("Project Purchase")
    STOCK = "STOCK", _("Stock Material")

class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    PENDING_APPROVAL = "PENDING_APPROVAL", _("Pending Approval")
    APPROVED = "APPROVED", _("Approved")
    REJECTED = "REJECTED", _("Rejected")
    CLOSED = "CLOSED", _("Closed")
    CANCELLED = "CANCELLED", _("Cancelled")

class PurchaseOrder(Timestampable, CompanyOwnedModel):
    number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("PO Number")
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="purchase_orders",
        verbose_name=_("Supplier")
    )
    po_type = models.CharField(
        max_length=20,
        choices=PurchaseOrderType.choices,
        default=PurchaseOrderType.PROJECT,
        verbose_name=_("Type")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_orders",
        verbose_name=_("Project File"),
        help_text=_("Required for project purchases; not used for stock material orders."),
    )
    external_reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name=_("Provider Reference"),
        help_text=_("External receipt/delivery note number from the provider (e.g. albarán number).")
    )
    date_ordered = models.DateField(
        default=timezone.now,
        verbose_name=_("Date Ordered")
    )
    status = models.CharField(
        max_length=20,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
        verbose_name=_("Status")
    )
    total_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Total Amount")
    )
    approved_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_pos",
        verbose_name=_("Approved By")
    )
    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Approved At")
    )
    rejected_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_pos",
        verbose_name=_("Rejected By")
    )
    rejected_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Rejected At")
    )
    rejection_reason = models.TextField(
        blank=True,
        verbose_name=_("Rejection Reason")
    )
    requested_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_pos",
        verbose_name=_("Requested By")
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    file = models.FileField(
        upload_to=po_file_path,
        null=True,
        blank=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=['pdf', 'png', 'jpg', 'jpeg']
            )
        ],
        verbose_name=_("Provider Quote/File"),
        help_text=_("Upload the quote from the provider to support approval.")
    )

    class Meta:
        verbose_name = _("Purchase Order")
        verbose_name_plural = _("Purchase Orders")
        ordering = ["-date_ordered", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"],
                name="uniq_purchase_order_company_number",
            ),
        ]
        permissions = [
            ("can_approve_purchase_order", _("Can approve Purchase Orders")),
            ("can_receive_directly", _("Can receive items directly")),
        ]

    def __str__(self):
        return f"{self.number or _('DRAFT')} - {self.provider.name}"

    def clean(self):
        super().clean()
        if self.po_type == PurchaseOrderType.PROJECT and not self.project_file:
            raise ValidationError({"project_file": _("A project file is required for project purchases.")})

    def save(self, *args, **kwargs):
        if self.po_type == PurchaseOrderType.STOCK:
            self.project_file = None
        if not self.number and self.company:
            self.number = next_scoped_code(
                model=PurchaseOrder,
                scope_filter={"company": self.company},
                prefix="PO",
                width=5,
                field_name="number"
            )
        super().save(*args, **kwargs)

    def recalculate_total(self):
        total = sum(item.line_total.amount for item in self.items.all())
        self.total_amount = total
        self.save(update_fields=["total_amount"])

    @property
    def billing_status(self):
        """
        Derived billing progress: not_invoiced / partially_invoiced / fully_invoiced.
        Sum comparison is valid because per-line over-allocation is blocked.
        """
        totals = self.items.aggregate(
            ordered=models.Sum("quantity"),
            invoiced=models.Sum("invoiced_quantity"),
        )
        ordered = totals["ordered"] or Decimal("0")
        invoiced = totals["invoiced"] or Decimal("0")
        if not ordered or invoiced <= 0:
            return "not_invoiced"
        if invoiced >= ordered:
            return "fully_invoiced"
        return "partially_invoiced"

    def check_and_update_status(self):
        """
        Closes the PO if all items are fully received. 
        Re-opens it if it was closed but items are now missing.
        """
        if self.status not in [PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.CLOSED]:
            return

        all_received = True
        has_items = False
        for item in self.items.all():
            has_items = True
            if item.received_quantity < item.quantity:
                all_received = False
                break
        
        if has_items:
            if all_received and self.status == PurchaseOrderStatus.APPROVED:
                self.status = PurchaseOrderStatus.CLOSED
                self.save(update_fields=["status"])
            elif not all_received and self.status == PurchaseOrderStatus.CLOSED:
                self.status = PurchaseOrderStatus.APPROVED
                self.save(update_fields=["status"])

class PurchaseOrderItem(Timestampable):
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Purchase Order")
    )
    inventory_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="po_lines",
        verbose_name=_("Item")
    )
    description = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Description")
    )
    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name=_("Quantity")
    )
    unit = models.CharField(
        max_length=2,
        choices=UnitChoices.choices,
        default=UnitChoices.UNITS,
        verbose_name=_("Unit"),
    )
    unit_price = MoneyField(
        max_digits=12,
        decimal_places=4,
        default_currency='EUR',
        verbose_name=_("Unit Price")
    )
    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency='EUR',
        editable=False,
        verbose_name=_("Line Total")
    )
    received_quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=0,
        verbose_name=_("Received Quantity")
    )
    invoiced_quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        default=0,
        verbose_name=_("Invoiced Quantity")
    )

    class Meta:
        verbose_name = _("PO Item")
        verbose_name_plural = _("PO Items")

    def __str__(self):
        return f"{self.inventory_item.name} ({self.quantity} {self.unit})"

    def recalculate_invoiced_quantity(self):
        """
        Recomputes invoiced_quantity from invoice allocations (self-healing,
        mirrors Payment.recalculate_pending_allocation()).
        """
        total = self.invoice_allocations.aggregate(total=models.Sum("quantity"))["total"] or Decimal("0")
        self.invoiced_quantity = total
        # Queryset update to skip PurchaseOrderItem.save() side effects
        # (PO total recalculation, inventory cost price sync).
        PurchaseOrderItem.objects.filter(pk=self.pk).update(invoiced_quantity=total)

    @property
    def pending_invoice_quantity(self):
        return self.quantity - self.invoiced_quantity

    def save(self, *args, **kwargs):
        if not self.description:
            self.description = self.inventory_item.name
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)
        self.purchase_order.recalculate_total()

        # Keep inventory item cost_price in sync with the latest purchase price
        if self.unit_price and self.unit_price.amount:
            self.inventory_item.cost_price = self.unit_price
            self.inventory_item.save(update_fields=["cost_price", "cost_price_currency"])

class PurchaseOrderReceipt(Timestampable, CompanyOwnedModel):
    number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Receipt Number")
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="receipts",
        verbose_name=_("Purchase Order")
    )
    warehouse = models.ForeignKey(
        Warehouse,
        on_delete=models.PROTECT,
        related_name="receipts",
        verbose_name=_("Warehouse")
    )
    received_at = models.DateTimeField(
        default=timezone.now,
        verbose_name=_("Received At")
    )
    received_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_receipts",
        verbose_name=_("Received By")
    )
    external_reference = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name=_("Provider Reference"),
        help_text=_("External receipt/delivery note number from the provider (e.g. albarán number).")
    )
    file = models.FileField(
        upload_to="purchasing/receipts/",
        null=True,
        blank=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=['pdf', 'png', 'jpg', 'jpeg']
            )
        ],
        verbose_name=_("Receipt Document"),
        help_text=_("Upload the provider's delivery note or receipt image.")
    )

    class Meta:
        verbose_name = _("PO Receipt")
        verbose_name_plural = _("PO Receipts")
        ordering = ["-received_at", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"],
                name="uniq_po_receipt_company_number",
            ),
        ]

    def clean(self):
        super().clean()
        if self.purchase_order_id and self.purchase_order.status != PurchaseOrderStatus.APPROVED:
            raise ValidationError(_("Receipts can only be created for APPROVED Purchase Orders."))

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.number and self.company:
            self.number = next_scoped_code(
                model=PurchaseOrderReceipt,
                scope_filter={"company": self.company},
                prefix="REC",
                width=5,
                field_name="number"
            )
        super().save(*args, **kwargs)

class PurchaseOrderReceiptItem(Timestampable):
    receipt = models.ForeignKey(
        PurchaseOrderReceipt,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Receipt")
    )
    po_item = models.ForeignKey(
        PurchaseOrderItem,
        on_delete=models.CASCADE,
        related_name="receipt_lines",
        verbose_name=_("PO Item")
    )
    quantity_received = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name=_("Quantity Received")
    )

    class Meta:
        verbose_name = _("Receipt Item")
        verbose_name_plural = _("Receipt Items")

    def __str__(self):
        return f"{self.po_item.inventory_item.name} - {self.quantity_received}"

    def clean(self):
        super().clean()
        if self.po_item_id:
            old_quantity = Decimal("0")
            if self.pk:
                # We need to fetch the current value from DB to calculate the diff accurately
                old_quantity = PurchaseOrderReceiptItem.objects.get(pk=self.pk).quantity_received
            
            # Check if total received would exceed ordered quantity
            total_after_save = self.po_item.received_quantity + (self.quantity_received - old_quantity)
            if total_after_save > self.po_item.quantity:
                raise ValidationError(
                    _("Cannot receive more than ordered. Ordered: %(ordered)s, Total Received: %(total)s") % {
                        "ordered": self.po_item.quantity,
                        "total": total_after_save
                    }
                )

    @transaction.atomic
    def save(self, *args, **kwargs):
        self.full_clean()
        is_new = self.pk is None
        old_quantity = 0
        if not is_new:
            old_quantity = PurchaseOrderReceiptItem.objects.get(pk=self.pk).quantity_received

        super().save(*args, **kwargs)

        # Update PO Item received quantity
        diff = self.quantity_received - old_quantity
        self.po_item.received_quantity += diff
        self.po_item.save(update_fields=["received_quantity"])

        # Stock Integration: Create ItemTransaction
        if self.po_item.inventory_item.item_type == ItemType.STOCKABLE:
            ItemTransaction.objects.create(
                item=self.po_item.inventory_item,
                warehouse=self.receipt.warehouse,
                movement_type=MovementType.IN,
                quantity_change=diff, # Note: using full diff (decimal)
                reference_object=self.receipt,
                user=self.receipt.received_by,
                note=_("Received via PO Receipt %s") % self.receipt.number
            )
        
        # Check if PO should be closed
        self.po_item.purchase_order.check_and_update_status()

    @transaction.atomic
    def delete(self, *args, **kwargs):
        # Update PO Item received quantity backwards
        self.po_item.received_quantity -= self.quantity_received
        self.po_item.save(update_fields=["received_quantity"])
        
        # Check if PO should be re-opened
        po = self.po_item.purchase_order
        
        super().delete(*args, **kwargs)
        po.check_and_update_status()
