import random
import string
from django.db import models, transaction
from decimal import Decimal
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from djmoney.models.fields import MoneyField
from timestamps.models import Timestampable
from company.models import CompanyOwnedModel
from materials.models import Provider, UnitChoices, PaymentMethod
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
    INVESTMENT = "INVESTMENT", _("Investment")
    MAINTENANCE = "MAINTENANCE", _("Maintenance")

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
        help_text=_("Required for project purchases; not used for stock, investment, or maintenance orders."),
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="purchase_orders",
        verbose_name=_("Cost Center"),
        help_text=_("Defaults to the provider's cost center when the order is created."),
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
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER,
        verbose_name=_("Payment Method"),
        help_text=_("Snapshot of the provider's payment method at creation; can be overridden per order."),
    )
    payment_terms_days = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_("Payment Terms (Days)"),
        help_text=_("Snapshot of the provider's payment terms at creation; can be overridden per order."),
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
        # Collected rather than raised one at a time, so the form reports every
        # problem in a single pass.
        errors = {}
        if self.po_type == PurchaseOrderType.PROJECT and not self.project_file:
            errors["project_file"] = _("A project file is required for project purchases.")
        if self.cost_center_id:
            if self.company_id and self.cost_center.company_id != self.company_id:
                errors["cost_center"] = _("Cost center must belong to the same company.")
            elif not self.cost_center.is_assignable:
                errors["cost_center"] = _(
                    "This cost center only groups its children; pick one of them instead."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.po_type != PurchaseOrderType.PROJECT:
            self.project_file = None
            
        if self._state.adding and self.provider_id:
            if not self.cost_center_id:
                default = self.provider.default_cost_center
                if default and default.is_active and default.is_assignable:
                    self.cost_center = default
            self.payment_method = self.provider.payment_method
            self.payment_terms_days = self.provider.payment_terms_days
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
    def total_amount_with_tax(self):
        """total_amount plus any taxes configured on each line's inventory item, if any."""
        from djmoney.money import Money

        total_tax = Decimal("0.00")
        for item in self.items.select_related("inventory_item").all():
            for tax in item.inventory_item.get_taxes():
                total_tax += tax.calculate(item.line_total.amount)["tax_amount"]
        return self.total_amount + Money(total_tax, self.total_amount.currency)

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

    def user_can_post_message(self, user):
        if not user or not user.is_authenticated:
            return False
        if self.requested_by_id and self.requested_by_id == user.pk:
            return True
        return user.has_perm("purchasing.can_approve_purchase_order")

    def user_can_view_conversation(self, user):
        # Grantable rather than hidden: view_purchaseordermessage opens the
        # thread to anyone who should follow it.
        return self.user_can_post_message(user) or (
            bool(user)
            and user.is_authenticated
            and user.has_perm("purchasing.view_purchaseordermessage")
        )

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
        Credit-note allocations subtract: they reverse previously invoiced quantity.
        """
        total = self.invoice_allocations.aggregate(
            total=models.Sum(
                models.Case(
                    models.When(invoice__document_type="credit_note", then=-models.F("quantity")),
                    default=models.F("quantity"),
                    output_field=models.DecimalField(max_digits=10, decimal_places=3),
                )
            )
        )["total"] or Decimal("0")
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

class PurchaseOrderMessageQuerySet(models.QuerySet):
    def from_approver_side(self, purchase_order):
        # Not re-checked against the approver permission: who may approve
        # changes over time, while who wrote a message does not.
        return self.exclude(sender_id=purchase_order.requested_by_id)


class PurchaseOrderMessage(Timestampable):
    """One conversation per purchase order. Scoped to a company through it."""
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name=_("Purchase Order")
    )
    sender = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_po_messages",
        verbose_name=_("Sender")
    )
    recipient = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="received_po_messages",
        verbose_name=_("Recipient"),
        help_text=_("Resolved when the message is sent; empty when it goes to the company's approver list.")
    )
    body = models.TextField(verbose_name=_("Message"))
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Read At")
    )

    objects = PurchaseOrderMessageQuerySet.as_manager()

    # How much of the thread a notification email repeats, so a long
    # conversation cannot fill an inbox.
    EMAIL_HISTORY_LIMIT = 5
    EMAIL_HISTORY_EXCERPT_CHARS = 400

    class Meta:
        verbose_name = _("Purchase Order Message")
        verbose_name_plural = _("Purchase Order Messages")
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["purchase_order", "created_at"]),
        ]

    def __str__(self):
        return f"{self.purchase_order.number}: {self.body[:50]}"

    def earlier_in_thread(self):
        """The messages posted before this one, newest first."""
        # Ordered by the same pair as Meta.ordering so messages sharing a
        # timestamp keep a stable order.
        return (
            self.purchase_order.messages
            .filter(
                models.Q(created_at__lt=self.created_at)
                | models.Q(created_at=self.created_at, pk__lt=self.pk)
            )
            .select_related("sender")
            .order_by("-created_at", "-pk")
        )

    def resolve_recipient(self):
        """Who owes the answer, or None to fall back to the approver list."""
        # The requester is checked first: superusers pass has_perm(), so an
        # approver-first test would make a superuser requester mail themselves.
        po = self.purchase_order
        if po.requested_by_id and self.sender_id == po.requested_by_id:
            last_asked = (
                po.messages.from_approver_side(po)
                .filter(sender__isnull=False)
                .order_by("-created_at", "-pk")
                .first()
            )
            return last_asked.sender if last_asked else None
        return po.requested_by

    def save(self, *args, **kwargs):
        if self._state.adding and self.recipient_id is None:
            self.recipient = self.resolve_recipient()
        super().save(*args, **kwargs)


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
        item = self.po_item.inventory_item
        if item.is_composite:
            # A composite holds no stock of its own, so what arrived are the
            # parts it is built from. Crediting the parent would trap the
            # goods: delivering it only ever takes its components out.
            item.restock_bom_components(
                quantity=diff,
                warehouse=self.receipt.warehouse,
                reference_object=self.receipt,
                user=self.receipt.received_by,
                note=_("Received via PO Receipt %s") % self.receipt.number,
            )
        elif item.item_type == ItemType.STOCKABLE:
            ItemTransaction.objects.create(
                item=item,
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


# ---------------------------------------------------------------------------
# Quote Requests (RFQ) — request pricing from multiple providers, compare and
# convert the winning ProviderQuote into a PurchaseOrder.
# ---------------------------------------------------------------------------

def _provider_quote_suffix(index):
    """
    Converts a zero-based index into a spreadsheet-style letter suffix:
    0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA, 27 -> AB, ...
    Used to build per-provider quote codes like QR-00001-A, QR-00001-B.
    """
    letters = ""
    n = index + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _generate_provider_quote_token():
    """
    Generates an 8-digit numeric access code for the public quote link.
    Kept short on purpose (easy to paste/share manually), while still
    being checked for uniqueness against existing ProviderQuote tokens.
    """
    while True:
        candidate = "".join(random.choices(string.digits, k=8))
        if not ProviderQuote.objects.filter(token=candidate).exists():
            return candidate


class QuoteRequestStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    REQUESTED = "REQUESTED", _("Quotes Created")
    CONVERTED = "CONVERTED", _("Converted to PO")


class ProviderQuoteStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    SENT = "SENT", _("Sent")
    RESPONDED = "RESPONDED", _("Responded")
    SELECTED = "SELECTED", _("Selected")
    REJECTED = "REJECTED", _("Rejected")


class QuoteRequest(Timestampable, CompanyOwnedModel):
    number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Request Number")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quote_requests",
        verbose_name=_("Project File"),
    )
    requested_by = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_quote_requests",
        verbose_name=_("Requested By")
    )
    date_requested = models.DateField(
        default=timezone.now,
        verbose_name=_("Date Requested")
    )
    status = models.CharField(
        max_length=20,
        choices=QuoteRequestStatus.choices,
        default=QuoteRequestStatus.DRAFT,
        verbose_name=_("Status")
    )
    notes = models.TextField(blank=True, verbose_name=_("Notes"))
    awarded_quote = models.ForeignKey(
        "ProviderQuote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Awarded Quote"),
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quote_request_source",
        verbose_name=_("Generated Purchase Order"),
    )

    class Meta:
        verbose_name = _("Quote Request")
        verbose_name_plural = _("Quote Requests")
        ordering = ["-date_requested", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"],
                name="uniq_quote_request_company_number",
            ),
        ]

    def __str__(self):
        return self.number or str(_("DRAFT"))

    def save(self, *args, **kwargs):
        if not self.number and self.company:
            self.number = next_scoped_code(
                model=QuoteRequest,
                scope_filter={"company": self.company},
                prefix="QR",
                width=5,
                field_name="number"
            )
        super().save(*args, **kwargs)


class QuoteRequestItem(Timestampable):
    quote_request = models.ForeignKey(
        QuoteRequest,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Quote Request")
    )
    inventory_item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name="quote_request_lines",
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

    class Meta:
        verbose_name = _("Quote Request Item")
        verbose_name_plural = _("Quote Request Items")

    def __str__(self):
        return f"{self.inventory_item.name} ({self.quantity} {self.unit})"

    def save(self, *args, **kwargs):
        if not self.description:
            self.description = self.inventory_item.name
        super().save(*args, **kwargs)


class ProviderQuote(Timestampable):
    quote_request = models.ForeignKey(
        QuoteRequest,
        on_delete=models.CASCADE,
        related_name="provider_quotes",
        verbose_name=_("Quote Request")
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="provider_quotes",
        verbose_name=_("Provider")
    )
    code = models.CharField(
        max_length=20,
        blank=True,
        editable=False,
        verbose_name=_("Code"),
        help_text=_("Auto-generated from the Quote Request (e.g. QR-00001-A).")
    )
    token = models.CharField(
        max_length=8,
        unique=True,
        editable=False,
        default=_generate_provider_quote_token,
        verbose_name=_("Access Code"),
        help_text=_("8-digit code embedded in the public link. Acts as the provider's access key."),
    )
    status = models.CharField(
        max_length=20,
        choices=ProviderQuoteStatus.choices,
        default=ProviderQuoteStatus.PENDING,
        verbose_name=_("Status")
    )
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Sent At"))
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Responded At"))
    shipping_cost = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Shipping Cost")
    )
    discount_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Discount")
    )
    valid_until = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Valid Until")
    )
    delivery_time_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Delivery Time (days)")
    )
    provider_notes = models.TextField(blank=True, verbose_name=_("Provider Notes"))
    total_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        editable=False,
        verbose_name=_("Total")
    )

    class Meta:
        verbose_name = _("Provider Quote")
        verbose_name_plural = _("Provider Quotes")
        ordering = ["quote_request", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["quote_request", "provider"],
                name="uniq_provider_quote_per_request",
            ),
        ]

    def __str__(self):
        return self.code or f"{self.quote_request} - {self.provider.name}"

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new and not self.code:
            sibling_ids = list(
                ProviderQuote.objects.filter(quote_request=self.quote_request)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            index = sibling_ids.index(self.pk)
            self.code = f"{self.quote_request.number}-{_provider_quote_suffix(index)}"
            ProviderQuote.objects.filter(pk=self.pk).update(code=self.code)

    def recalculate_total(self):
        lines_total = sum(
            (line.line_total.amount for line in self.items.all() if line.line_total is not None),
            start=Decimal("0"),
        )
        shipping = self.shipping_cost.amount if self.shipping_cost else Decimal("0")
        discount = self.discount_amount.amount if self.discount_amount else Decimal("0")
        total = lines_total + shipping - discount
        if total < 0:
            total = Decimal("0")
        self.total_amount = total
        self.save(update_fields=["total_amount"])

    @property
    def is_complete(self):
        return self.items.exists() and not self.items.filter(unit_price__isnull=True).exists()

    def mark_as_sent(self):
        if self.status == ProviderQuoteStatus.PENDING:
            self.status = ProviderQuoteStatus.SENT
            self.sent_at = timezone.now()
            self.save(update_fields=["status", "sent_at"])

    def queue_quote_email(self, language="es", user=None):
        """
        Validates the provider has an email on file (synchronously, so
        the caller can show a friendly error right away), then queues
        the actual email send in Celery via purchasing.tasks — the
        SMTP round trip and the mark_as_sent() call both happen inside
        that task, not here. `language` is "es" or "en".

        `user` is whoever asked for the quote: their address becomes the
        Reply-To, so the provider answers them and not the shared mailbox.
        """
        if not self.provider.email:
            raise ValidationError(_("This provider has no email on file."))

        from general.email import sender_reply_to
        from .tasks import send_provider_quote_email

        send_provider_quote_email.delay(
            self.pk, language, reply_to=sender_reply_to(user)
        )

    @transaction.atomic
    def convert_to_purchase_order(self, user=None):
        """Creates a PurchaseOrder from this quote, rejects the sibling quotes."""
        if self.quote_request.status == QuoteRequestStatus.CONVERTED:
            raise ValidationError(_("This quote request has already been converted to a Purchase Order."))
        if not self.is_complete:
            raise ValidationError(_("Cannot select a quote with pending prices."))

        extra_notes = []
        if self.quote_request.notes:
            extra_notes.append(self.quote_request.notes)
        if self.shipping_cost and self.shipping_cost.amount:
            extra_notes.append(str(_("Quote shipping cost: %(amount)s") % {"amount": self.shipping_cost}))
        if self.discount_amount and self.discount_amount.amount:
            extra_notes.append(str(_("Discount applied: %(amount)s") % {"amount": self.discount_amount}))
        if self.delivery_time_days:
            extra_notes.append(str(_("Delivery time quoted: %(days)s days") % {"days": self.delivery_time_days}))
        if self.valid_until:
            extra_notes.append(str(_("Quote valid until: %(date)s") % {"date": self.valid_until}))

        po = PurchaseOrder.objects.create(
            company=self.quote_request.company,
            provider=self.provider,
            project_file=self.quote_request.project_file,
            requested_by=user or self.quote_request.requested_by,
            notes="\n".join(extra_notes),
        )
        for line in self.items.select_related("quote_request_item"):
            PurchaseOrderItem.objects.create(
                purchase_order=po,
                inventory_item=line.quote_request_item.inventory_item,
                description=line.description,
                quantity=line.quantity,
                unit=line.unit,
                unit_price=line.unit_price,
            )

        self.status = ProviderQuoteStatus.SELECTED
        self.save(update_fields=["status"])
        self.quote_request.provider_quotes.exclude(pk=self.pk).update(status=ProviderQuoteStatus.REJECTED)

        self.quote_request.status = QuoteRequestStatus.CONVERTED
        self.quote_request.awarded_quote = self
        self.quote_request.purchase_order = po
        self.quote_request.save(update_fields=["status", "awarded_quote", "purchase_order"])

        return po


class ProviderQuoteItem(Timestampable):
    provider_quote = models.ForeignKey(
        ProviderQuote,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Provider Quote")
    )
    quote_request_item = models.ForeignKey(
        QuoteRequestItem,
        on_delete=models.CASCADE,
        related_name="provider_lines",
        verbose_name=_("Requested Line")
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
        null=True,
        blank=True,
        verbose_name=_("Unit Price")
    )
    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency='EUR',
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Line Total")
    )

    class Meta:
        verbose_name = _("Provider Quote Item")
        verbose_name_plural = _("Provider Quote Items")

    def __str__(self):
        return f"{self.description} ({self.quantity} {self.unit})"

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity if self.unit_price is not None else None
        super().save(*args, **kwargs)
        self.provider_quote.recalculate_total()