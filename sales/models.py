import hashlib

from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone
from djmoney.models.fields import MoneyField
from materials.models import Material
from timestamps.models import Timestampable
from django.utils.translation import gettext_lazy as _
from company.models import CompanyOwnedModel
from decimal import Decimal
from django.contrib.contenttypes.models import ContentType
from invoices.models import AppliedTax, PaymentMethod
from company.models import Company, CompanyOwnedModel, BaseBankAccount, BankAccount
from django_countries.fields import CountryField

class SaleZone(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))

    class Meta:
        verbose_name = _("Sale Zone")
        verbose_name_plural = _("Sale Zones")

    def __str__(self):
        return f"{self.name} ({self.company})"

class ClientBankAccount(CompanyOwnedModel, BaseBankAccount):
    client = models.ForeignKey(
        'Client',
        on_delete=models.CASCADE,
        related_name="client_bank_accounts",
        verbose_name=_("Client")
    )
    is_default = models.BooleanField(default=False, verbose_name=_("Is Default"))

    class Meta:
        verbose_name = _("Client Bank Account")
        verbose_name_plural = _("Client Bank Accounts")

    def __str__(self):
        return f"{self.bank_name} - {self.account_number or self.iban} ({self.client})"

class ClientAddress(CompanyOwnedModel):
    ADDRESS_TYPES = [
        ('fiscal', _('Fiscal')),
        ('shipping', _('Shipping')),
        ('other', _('Other')),
    ]
    client = models.ForeignKey(
        'Client',
        on_delete=models.CASCADE,
        related_name="addresses",
        verbose_name=_("Client")
    )
    address_type = models.CharField(
        max_length=20,
        choices=ADDRESS_TYPES,
        default='shipping',
        verbose_name=_("Address Type")
    )
    street = models.CharField(max_length=255, verbose_name=_("Street"))
    city = models.CharField(max_length=100, verbose_name=_("City"))
    postal_code = models.CharField(max_length=20, verbose_name=_("Postal Code"))
    province = models.CharField(max_length=100, verbose_name=_("Province"))
    country = CountryField(default="ES", verbose_name=_("Country"))

    class Meta:
        verbose_name = _("Client Address")
        verbose_name_plural = _("Client Addresses")

    def __str__(self):
        return f"{self.get_address_type_display()}: {self.street}, {self.city}"

class ClientContact(models.Model):
    client = models.ForeignKey(
        'Client',
        on_delete=models.CASCADE,
        related_name="contacts",
        verbose_name=_("Client"),
        null=True,
        blank=True
    )
    address = models.ForeignKey(
        ClientAddress,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contacts",
        verbose_name=_("Address")
    )
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    position = models.CharField(max_length=100, blank=True, verbose_name=_("Position"))
    email = models.EmailField(blank=True, verbose_name=_("Email"))
    phone = models.CharField(max_length=50, blank=True, verbose_name=_("Phone"))

    class Meta:
        verbose_name = _("Client Contact")
        verbose_name_plural = _("Client Contacts")

    def __str__(self):
        return f"{self.name} ({self.position})"

class Client(Timestampable, CompanyOwnedModel):
    # Existing fields
    name = models.CharField(
        max_length=200,
        help_text=_("e.g., SEBASTIAN HERRERO"),
        verbose_name=_("Name"),
    )
    phone = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Phone"),
    )
    email = models.EmailField(
        blank=True,
        verbose_name=_("Email"),
    )
    address = models.TextField(
        blank=True,
        verbose_name=_("Address"),
        help_text=_("Main or legacy address")
    )
    tax_identifier = models.CharField(
        max_length=25,
        blank=True,
        verbose_name=_("Fiscal Identifier"),
    )
    industry_identifier = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_("Industry Identifier"),
        help_text=_("e.g., CNAE, SIC code, or similar industry classification"),
    )

    # New fields
    sale_zone = models.ForeignKey(
        SaleZone,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clients",
        verbose_name=_("Sale Zone")
    )
    agent = models.ForeignKey(
        'company.Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="managed_clients",
        verbose_name=_("Agent")
    )
    agent_commission = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name=_("Agent Commission (%)")
    )
    is_insured = models.BooleanField(
        default=False,
        verbose_name=_("Is Insured")
    )
    max_risk = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Maximum Risk")
    )
    current_risk = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Current Risk")
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER,
        verbose_name=_("Payment Method")
    )
    our_bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clients_as_deposit",
        verbose_name=_("Our Bank Account (for deposit)")
    )
    default_client_bank_account = models.ForeignKey(
        ClientBankAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="is_default_for_client",
        verbose_name=_("Default Client Bank Account")
    )

    # Términos y condiciones
    terms_accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("T&C Accepted At"),
    )
    terms_signature = models.ImageField(
        upload_to="client_terms_signatures/",
        blank=True,
        null=True,
        verbose_name=_("Firma T&C"),
    )
    accepted_terms_company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="client_terms_acceptances",
        verbose_name=_("T&C company reference"),
        help_text=_("Company whose terms were accepted; immutable once set."),
    )
    accepted_terms_text = models.TextField(
        blank=True,
        editable=False,
        verbose_name=_("T&C text snapshot"),
        help_text=_("Copy of the company terms at acceptance time."),
    )

    class Meta:
        verbose_name = _("Client")
        verbose_name_plural = _("Clients")

    def __str__(self):
        return self.name

    @property
    def terms_version(self) -> str:
        text = (self.accepted_terms_text or "").strip()
        if not text:
            return ""
        return hashlib.sha256(text.encode()).hexdigest()[:8]

    
class OrderStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    CONFIRMED = "confirmed", _("Confirmed")
    PRODUCTION = "production", _("In production")
    COMPLETED = "completed", _("Completed")

class Order(CompanyOwnedModel, Timestampable):
    """
    Represents the Excel File itself (the header info).
    """

    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        verbose_name=_("Client"),
    )

    project_name = models.CharField(
        max_length=200,
        help_text=_("Work / site name"),
        verbose_name=_("Project name"),
    )

    date_created = models.DateField(
        auto_now_add=True,
        verbose_name=_("Created date"),
    )

    deadline_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Deadline"),
    )

    status = models.CharField(
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.DRAFT,
        verbose_name=_("Status"),
    )

    total_cost_material = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Material cost"),
    )

    total_cost_labor = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Labor cost"),
    )

    total_cost_machine = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Machine cost"),
    )

    final_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        editable=False,
        verbose_name=_("Final price"),
    )

    class Meta:
        verbose_name = _("Order")
        verbose_name_plural = _("Orders")

    def __str__(self):
        return f"{self.project_name} – {self.client}"

    def recalc_totals(self):
        items = self.items.all()
        total = sum(item.line_total.amount for item in items)

        self.total_material_cost = total
        self.final_price = self.total_material_cost + self.total_labor_cost
        self.save(update_fields=["total_material_cost", "final_price"])

    
class OrderItem(Timestampable):
    """
    A line item inside an order.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Order"),
    )

    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        help_text=_("Material used"),
        verbose_name=_("Material"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name=_("Quantity"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        help_text=_("Price per unit"),
        verbose_name=_("Unit price"),
    )

    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        editable=False,
        verbose_name=_("Line total"),
    )

    class Meta:
        verbose_name = _("Order item")
        verbose_name_plural = _("Order items")

    def __str__(self):
        return f"{self.material} × {self.quantity}"

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)


    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)

class QuoteStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SENT = "sent", _("Sent")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")


class Quote(Timestampable, CompanyOwnedModel):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("Client"),
    )

    project_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Project name"),
    )

    date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Date"),
    )

    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quotes",
        verbose_name=_("Project File"),
    )

    expires_at = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Expires at"),
    )

    status = models.CharField(
        max_length=20,
        choices=QuoteStatus.choices,
        default=QuoteStatus.DRAFT,
        verbose_name=_("Status"),
    )

    subtotal_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        default=0,
        verbose_name=_("Subtotal price"),
    )

    tax_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        default=0,
        verbose_name=_("Tax amount"),
    )

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        default=0,
        verbose_name=_("Total price"),
    )

    tax_breakdown = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_("Tax breakdown"),
        help_text=_("Stores the aggregated tax bases and amounts"),
    )

    observations = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Observations"),
    )

    internal_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Internal name"),
        help_text=_("Stable name shown in the Fislav app; not affected by PDF renaming."),
    )

    date_sent = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Date sent"),
    )

    date_accepted = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Date accepted"),
    )

    date_rejected = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Date rejected"),
    )

    class Meta:
        verbose_name = _("Quote")
        verbose_name_plural = _("Quotes")
        permissions = [
            ("can_view_quote_dashboard", _("Can view quote dashboard")),
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._original_status = self.status

    @transaction.atomic
    def save(self, *args, **kwargs):
        if not self.internal_name and self.project_name:
            self.internal_name = self.project_name

        # Auto-set date fields on status transitions
        if self.status != self._original_status:
            from django.utils import timezone
            now = timezone.now()

            if self.status == QuoteStatus.SENT and not self.date_sent:
                self.date_sent = now
            elif self.status == QuoteStatus.ACCEPTED and not self.date_accepted:
                self.date_accepted = now
            elif self.status == QuoteStatus.REJECTED and not self.date_rejected:
                self.date_rejected = now

        super().save(*args, **kwargs)

        # Subtract inventory when the quote is accepted
        if (
            self.status == QuoteStatus.ACCEPTED
            and self._original_status != QuoteStatus.ACCEPTED
        ):
            self._deduct_inventory()

        self._original_status = self.status

    def _deduct_inventory(self):
        """Create stock-out ItemTransactions for all stockable QuoteInventoryItems."""
        from inventory.models import ItemTransaction
        from inventory.choices import ItemType, MovementType

        for line in self.inventory_items.select_related(
            "inventory_item", "inventory_item__default_warehouse"
        ):
            item = line.inventory_item
            if item.item_type != ItemType.STOCKABLE:
                continue
            if not item.default_warehouse:
                continue

            ItemTransaction.objects.create(
                item=item,
                warehouse=item.default_warehouse,
                movement_type=MovementType.OUT,
                quantity_change=-int(line.quantity),
                reference_object=self,
                note=f"Stock out from accepted Quote #{self.id}",
            )

    def __str__(self):
        return self.project_name or f"Quote #{self.id}"

    def recalc_totals(self):
        if not self.id:
            return

        item_total, config_subtotal = self._calculate_subtotal()
        self._sync_applied_taxes()
        tax_amount, tax_breakdown = self._build_tax_breakdown()
        total = self._calculate_total(item_total)

        self.subtotal_price = item_total + config_subtotal
        self.tax_amount = tax_amount
        self.tax_breakdown = tax_breakdown
        self.total_price = total
        self.save(update_fields=['subtotal_price', 'tax_amount', 'total_price', 'tax_breakdown'])

    def _calculate_subtotal(self):
        """Returns (item_total, config_subtotal) as raw Decimals."""
        item_total = sum(
            (item.line_total.amount for item in self.items.all()),
            Decimal("0.00"),
        )
        # Include inventory-item lines
        inv_item_total = sum(
            (item.line_total.amount for item in self.inventory_items.all()),
            Decimal("0.00"),
        )
        config_subtotal = sum(
            (c.subtotal_price.amount for c in self.configurations.all()),
            Decimal("0.00"),
        )
        return item_total + inv_item_total, config_subtotal

    def _sync_applied_taxes(self):
        """
        Aggregates AppliedTax records from all linked configurations
        and inventory-item lines, then syncs them as Quote-level AppliedTax records.
        """
        ct = ContentType.objects.get_for_model(self)

        # Aggregate taxes across all configurations
        aggregation = {}  # key: (tax_type_id, percentage) -> {tax_type, base, calc, order}
        for config in self.configurations.all():
            for tax in config.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                if key not in aggregation:
                    aggregation[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                        "order": tax.order,
                    }
                # Use tax_breakdown snapshot values as fallback for older configs
                # where base_amount / calculated_amount may be null
                base = tax.base_amount.amount if tax.base_amount else Decimal("0.00")
                calc = tax.calculated_amount.amount if tax.calculated_amount else Decimal("0.00")

                # If the config has a tax_breakdown with amounts, prefer those
                # (they're always in sync with total_price)
                for tb in config.tax_breakdown or []:
                    if (tb.get('tax_name') == tax.tax_type.tax_name
                            and Decimal(str(tb.get('percentage', 0))) == tax.selected_percentage):
                        base = Decimal(str(tb.get('base', base)))
                        calc = Decimal(str(tb.get('tax_amount', calc)))
                        break

                aggregation[key]["base_amount"] += base
                aggregation[key]["calculated_amount"] += calc

        # Aggregate taxes from inventory-item lines
        for inv_item in self.inventory_items.all():
            for tax in inv_item.get_taxes():
                key = (tax.tax_type_id, tax.selected_percentage)
                line_base = inv_item.line_total.amount if inv_item.line_total else Decimal("0.00")
                rate = tax.selected_percentage / Decimal("100")
                line_calc = (line_base * rate).quantize(Decimal("0.01"))

                if key not in aggregation:
                    aggregation[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                        "order": tax.order,
                    }
                aggregation[key]["base_amount"] += line_base
                aggregation[key]["calculated_amount"] += line_calc

        # Upsert Quote-level AppliedTax records
        seen_ids = []
        for (_, _), agg in aggregation.items():
            applied, _ = AppliedTax.objects.update_or_create(
                content_type=ct,
                object_id=self.pk,
                tax_type=agg["tax_type"],
                selected_percentage=agg["selected_percentage"],
                defaults={
                    "base_amount": agg["base_amount"].quantize(Decimal("0.01")),
                    "calculated_amount": agg["calculated_amount"].quantize(Decimal("0.01")),
                    "order": agg["order"],
                },
            )
            seen_ids.append(applied.pk)

        # Remove stale taxes no longer present in any configuration or inventory item
        AppliedTax.objects.filter(
            content_type=ct,
            object_id=self.pk,
        ).exclude(pk__in=seen_ids).delete()

    def _build_tax_breakdown(self):
        """
        Derives the tax_breakdown JSON and total tax amount
        from the Quote's own AppliedTax records (single source of truth).
        """
        ct = ContentType.objects.get_for_model(self)
        taxes = AppliedTax.objects.filter(
            content_type=ct,
            object_id=self.pk,
        ).select_related("tax_type").order_by("order")

        breakdown = []
        total_tax = Decimal("0.00")
        for tax in taxes:
            calc = tax.calculated_amount.amount if tax.calculated_amount else Decimal("0.00")
            base = tax.base_amount.amount if tax.base_amount else Decimal("0.00")
            breakdown.append({
                "tax_name": tax.tax_type.tax_name,
                "percentage": str(tax.selected_percentage),
                "base": str(base.quantize(Decimal("0.01"))),
                "tax_amount": str(calc.quantize(Decimal("0.01"))),
            })
            total_tax += calc
        return total_tax, breakdown

    def _calculate_total(self, item_total):
        """
        Total = sum of config totals (already include taxes)
              + loose item total (QuoteItem + QuoteInventoryItem line_totals, no tax)
              + inventory item tax amounts only (line_total already in item_total).
        """
        config_total = sum(
            (c.total_price.amount for c in self.configurations.all()),
            Decimal("0.00"),
        )
        # Only add the tax delta — line_total is already included in item_total
        inv_item_tax_only = sum(
            (inv.total_with_tax.amount - inv.line_total.amount
             for inv in self.inventory_items.all()
             if inv.total_with_tax and inv.line_total),
            Decimal("0.00"),
        )
        return config_total + item_total + inv_item_tax_only

class QuoteConfigurationGroup(Timestampable):
    """
    An optional organisational group that clusters Configurations within a Quote.
    Purely presentational — does not affect the Quote's financial totals.
    """
    quote = models.ForeignKey(
        Quote,
        on_delete=models.CASCADE,
        related_name="configuration_groups",
        verbose_name=_("Quote"),
    )

    name = models.CharField(
        max_length=200,
        verbose_name=_("Name"),
    )

    notes = models.TextField(
        blank=True,
        verbose_name=_("Notes"),
    )

    order = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Order"),
        db_index=True,
    )

    class Meta:
        verbose_name = _("Configuration Group")
        verbose_name_plural = _("Configuration Groups")
        ordering = ["order", "id"]

    def __str__(self):
        return self.name

    @property
    def subtotal(self):
        """Sum of subtotal_price of all member configurations."""
        return sum(
            (c.subtotal_price.amount for c in self.configurations.all()),
            Decimal("0.00"),
        )

    @property
    def total(self):
        """Sum of total_price (after tax) of all member configurations."""
        return sum(
            (c.total_price.amount for c in self.configurations.all()),
            Decimal("0.00"),
        )

    @property
    def unit_price(self):
        """Sum of unit_price of all member configurations."""
        return sum(
            (c.unit_price.amount for c in self.configurations.all()),
            Decimal("0.00"),
        )

    @property
    def price_total(self):
        """Sum of component-group price_total across all member configurations."""
        total = Decimal("0.00")
        for config in self.configurations.prefetch_related("component_groups__items").all():
            for cg in config.component_groups.all():
                total += cg.price_total
        return total


class QuoteItem(Timestampable):
    quote = models.ForeignKey(
        Quote,
        related_name="items",
        on_delete=models.CASCADE,
        verbose_name=_("Quote"),
    )

    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        verbose_name=_("Material"),
    )

    category = models.ForeignKey(
        "materials.MaterialFamily",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("Category"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name=_("Quantity"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        verbose_name=_("Unit price"),
    )

    discount = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        verbose_name=_("Discount (%)"),
    )

    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        editable=False,
        default=0,
        verbose_name=_("Line total"),
    )

    class Meta:
        verbose_name = _("Quote item")
        verbose_name_plural = _("Quote items")

    def __str__(self):
        return self.material.name if self.material_id else f"QuoteItem #{self.pk}"

    def save(self, *args, **kwargs):
        discount_factor = (Decimal("100") - self.discount) / Decimal("100")
        self.line_total = self.unit_price * self.quantity * discount_factor
        super().save(*args, **kwargs)
        self.quote.recalc_totals()


class QuoteInventoryItem(Timestampable):
    """A line item inside a Quote that references an InventoryItem."""

    quote = models.ForeignKey(
        Quote,
        related_name="inventory_items",
        on_delete=models.CASCADE,
        verbose_name=_("Quote"),
    )

    inventory_item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        verbose_name=_("Inventory Item"),
    )

    quantity = models.DecimalField(
        max_digits=10,
        decimal_places=3,
        verbose_name=_("Quantity"),
    )

    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Unit price"),
    )

    discount = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        verbose_name=_("Discount (%)"),
    )

    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        editable=False,
        default=0,
        verbose_name=_("Line total"),
    )

    total_with_tax = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        editable=False,
        default=0,
        verbose_name=_("Total with tax"),
    )

    class Meta:
        verbose_name = _("Quote inventory item")
        verbose_name_plural = _("Quote inventory items")

    def __str__(self):
        if self.inventory_item_id:
            return f"{self.inventory_item.name} × {self.quantity}"
        return f"QuoteInventoryItem #{self.pk}"

    def save(self, *args, **kwargs):
        # Auto-fill unit_price from the item's selling_price when not provided
        if self.inventory_item_id and (not self.unit_price or self.unit_price.amount == 0):
            sp = self.inventory_item.selling_price
            if sp and sp.amount:
                self.unit_price = sp

        discount_factor = (Decimal("100") - self.discount) / Decimal("100")
        self.line_total = self.unit_price * self.quantity * discount_factor
        is_new = self.pk is None
        super().save(*args, **kwargs)

        if is_new and self.inventory_item_id:
            self._copy_taxes_from_item()

        # Recalculate stored total_with_tax from per-line taxes
        self._recalc_total_with_tax()
        self.quote.recalc_totals()

    def _recalc_total_with_tax(self):
        """Recompute total_with_tax from line_total + per-line applied taxes."""
        line = self.line_total.amount if self.line_total else Decimal("0.00")
        line_tax = Decimal("0.00")
        for tax in self.get_taxes():
            rate = tax.selected_percentage / Decimal("100")
            if tax.tax_type.behavior == "add":
                line_tax += (line * rate).quantize(Decimal("0.01"))
            else:
                line_tax -= (line * rate).quantize(Decimal("0.01"))
        self.total_with_tax = line + line_tax
        super().save(update_fields=["total_with_tax", "total_with_tax_currency"])

    def _copy_taxes_from_item(self):
        """Copy AppliedTax rows from the source InventoryItem to this line."""
        from invoices.models import AppliedTax
        ct = ContentType.objects.get_for_model(self)
        for src_tax in self.inventory_item.get_taxes():
            AppliedTax.objects.get_or_create(
                content_type=ct,
                object_id=self.pk,
                tax_type=src_tax.tax_type,
                selected_percentage=src_tax.selected_percentage,
                defaults={"order": src_tax.order},
            )

    def get_taxes(self):
        """Return AppliedTax records attached to this line via GenericFK."""
        from invoices.models import AppliedTax
        ct = ContentType.objects.get_for_model(self)
        return AppliedTax.objects.filter(
            content_type=ct, object_id=self.pk
        ).select_related("tax_type").order_by("order")


class OrderManager(Timestampable):
    def create_from_quote(self, quote):
        order = self.create(
            client=quote.client,
            project_name=quote.project_name,
        )
        for item in quote.items.all():
            OrderItem.objects.create(
                order=order,
                material=item.material,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
        order.recalc_totals()
        return order

def _quote_photo_path(instance, filename):
    from django.utils import timezone
    ts = int(timezone.now().timestamp())
    return f"quotes/photos/{instance.quote.company_id}/{instance.quote_id}/{ts}_{filename}"


class QuotePhotoCategory(CompanyOwnedModel):
    name = models.CharField(max_length=100, verbose_name=_("Name"))
    order = models.PositiveIntegerField(default=0, verbose_name=_("Order"), db_index=True)
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))

    class Meta:
        verbose_name = _("Quote Photo Category")
        verbose_name_plural = _("Quote Photo Categories")
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class QuotePhoto(models.Model):
    quote = models.ForeignKey(
        Quote,
        on_delete=models.CASCADE,
        related_name="photos",
        verbose_name=_("Quote"),
    )
    category = models.ForeignKey(
        QuotePhotoCategory,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="photos",
        verbose_name=_("Category"),
    )
    image = models.ImageField(upload_to=_quote_photo_path, verbose_name=_("Image"))
    caption = models.TextField(blank=True, verbose_name=_("Caption"))
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Uploaded at"))

    class Meta:
        verbose_name = _("Quote Photo")
        verbose_name_plural = _("Quote Photos")
        ordering = ["uploaded_at"]

    def __str__(self):
        cat = self.category.name if self.category else "—"
        return f"Photo [{cat}] – Quote #{self.quote_id}"


class DeliveryOrderStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SENT = "sent", _("Sent")
    DELIVERED = "delivered", _("Delivered")
    CANCELLED = "cancelled", _("Cancelled")

class DeliveryOrder(Timestampable, CompanyOwnedModel):
    number = models.CharField(max_length=50, blank=True, verbose_name=_("Number"))
    date = models.DateField(default=timezone.now, verbose_name=_("Date"))
    client = models.ForeignKey(
        Client, 
        on_delete=models.CASCADE, 
        related_name="delivery_orders",
        verbose_name=_("Client")
    )
    incremental_id = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Incremental ID"))
    sales_invoice = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
        verbose_name=_("Sales Invoice")
    )
    configuration = models.ForeignKey(
        "configurator.Configuration",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
        verbose_name=_("Configuration")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delivery_orders",
        verbose_name=_("Project File")
    )
    status = models.CharField(
        max_length=20,
        choices=DeliveryOrderStatus.choices,
        default=DeliveryOrderStatus.DRAFT,
        verbose_name=_("Status")
    )

    class Meta:
        verbose_name = _("Delivery Order")
        verbose_name_plural = _("Delivery Orders")
        ordering = ["-date", "-number"]
        unique_together = ["company", "number"]

    def __str__(self):
        return f"{self.number or 'Draft'} - {self.client}"

class DeliveryOrderItem(Timestampable):
    delivery_order = models.ForeignKey(
        DeliveryOrder,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("Delivery Order")
    )
    description = models.CharField(max_length=255, verbose_name=_("Description"))
    quantity = models.DecimalField(max_digits=10, decimal_places=3, verbose_name=_("Quantity"))

    class Meta:
        verbose_name = _("Delivery Order Item")
        verbose_name_plural = _("Delivery Order Items")

    def __str__(self):
        return f"{self.description} ({self.quantity})"


class ConversionThresholds(models.Model):
    """
    Per-company configuration for the conversion diagnosis thresholds.

    Each field is an absolute conversion % boundary (0–100).
    The diagnosis logic applied in analysis.py is:

        conv == 0                              → Sin ventas
        0 < conv <= critical_pct              → Crítico
        critical_pct < conv <= very_low_pct   → Muy bajo
        very_low_pct < conv <= low_pct        → Bajo
        low_pct < conv <= improve_pct         → Por mejorar
        improve_pct < conv <= acceptable_pct  → Aceptable
        acceptable_pct < conv <= good_pct     → Bueno
        conv > good_pct                       → Excelente

    Default values mirror the original relative-to-average thresholds
    but expressed as concrete percentages for a 60 % average:
        critical  ≈ avg × 0.15  →  9 %
        very_low  ≈ avg × 0.45  → 27 %
        low       ≈ avg × 0.75  → 45 %
        improve   ≈ avg × 1.00  → 60 %  (anything below average)
        acceptable≈ avg × 1.25  → 75 %
        good      ≈ avg × 1.75  → 100 % (anything above → Excelente)
    """

    company = models.OneToOneField(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="conversion_thresholds",
        verbose_name=_("Company"),
    )

    critical_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("9.00"),
        verbose_name=_("Crítico (hasta %)"),
        help_text=_(
            "Conversiones iguales o por debajo de este % se clasifican como Crítico. "
            "Por ejemplo: 9 significa que una conversión del 0–9 % es Crítico."
        ),
    )
    very_low_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("27.00"),
        verbose_name=_("Muy bajo (hasta %)"),
        help_text=_(
            "Conversiones hasta este % se clasifican como Muy bajo."
        ),
    )
    low_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("45.00"),
        verbose_name=_("Bajo (hasta %)"),
        help_text=_(
            "Conversiones hasta este % se clasifican como Bajo."
        ),
    )
    improve_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("60.00"),
        verbose_name=_("Por mejorar (hasta %)"),
        help_text=_(
            "Conversiones hasta este % se clasifican como Por mejorar. "
            "Normalmente debería coincidir con vuestra tasa de conversión media objetivo."
        ),
    )
    acceptable_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("75.00"),
        verbose_name=_("Aceptable (hasta %)"),
        help_text=_(
            "Conversiones hasta este % se clasifican como Aceptable."
        ),
    )
    good_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("100.00"),
        verbose_name=_("Bueno (hasta %)"),
        help_text=_(
            "Conversiones hasta este % se clasifican como Bueno. "
            "Por encima de este valor → Excelente."
        ),
    )

    class Meta:
        verbose_name = _("Umbrales de Conversión")
        verbose_name_plural = _("Umbrales de Conversión")

    def __str__(self):
        return f"Umbrales de conversión — {self.company}"