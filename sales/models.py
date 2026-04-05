from django.db import models
from django.utils import timezone
from djmoney.models.fields import MoneyField
from materials.models import Material
from timestamps.models import Timestampable
from django.utils.translation import gettext_lazy as _
from company.models import CompanyOwnedModel
from decimal import Decimal
from django.contrib.contenttypes.models import ContentType
from invoices.models import AppliedTax, PaymentMethod
from company.models import CompanyOwnedModel, BaseBankAccount, BankAccount
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
    phone = models.CharField(max_length=20, blank=True, verbose_name=_("Phone"))

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
        max_length=20,
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

    class Meta:
        verbose_name = _("Client")
        verbose_name_plural = _("Clients")

    def __str__(self):
        return self.name

    
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


class Quote(Timestampable, CompanyOwnedModel):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        verbose_name=_("Client"),
    )

    project_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Project name"),
    )

    expires_at = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("Expires at"),
    )

    status = models.CharField(
        max_length=20,
        choices=[
            ("draft", _("Draft")),
            ("sent", _("Sent")),
            ("accepted", _("Accepted")),
            ("rejected", _("Rejected")),
        ],
        default="draft",
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

    class Meta:
        verbose_name = _("Quote")
        verbose_name_plural = _("Quotes")

    def __str__(self):
        return f"{_('Quote')} #{self.id} – {self.client} - {self.project_name}"

    def recalc_totals(self):
        if not self.id:
            return

        

        # Calculate Subtotal
        item_total = sum(item.line_total.amount for item in self.items.all())
        config_subtotal = sum(c.subtotal_price.amount for c in self.configurations.all())
        self.subtotal_price = item_total + config_subtotal

        breakdown_dict = {}

        # Aggregate Configuration Taxes
        for c in self.configurations.all():
            for tax in c.tax_breakdown or []:
                key = f"{tax['tax_name']}_{tax['percentage']}"
                if key not in breakdown_dict:
                    breakdown_dict[key] = {
                        "tax_name": tax['tax_name'],
                        "percentage": tax['percentage'],
                        "base": Decimal("0.00"),
                        "tax_amount": Decimal("0.00"),
                    }
                breakdown_dict[key]["base"] += Decimal(str(tax.get('base', 0)))
                breakdown_dict[key]["tax_amount"] += Decimal(str(tax.get('tax_amount', 0)))

        # 2. Quote-level taxes (Applied to QuoteItem subtotal, or overall if needed)
        # Note: If Quote has AppliedTax, we apply them to item_total (since configs have their own taxes)
        # Or, we could apply Quote taxes to the whole subtotal if the user configures it that way.
        # Assuming Quote AppliedTaxes apply to the `item_total` (loose items):
        ct = ContentType.objects.get_for_model(self)
        quote_taxes = AppliedTax.objects.filter(content_type=ct, object_id=self.pk).order_by('order', 'tax_type')

        current_item_total = Decimal(str(item_total))
        quote_tax_total = Decimal("0.00")

        for tax in quote_taxes:
            result = tax.calculate(current_item_total)
            tax.base_amount = current_item_total
            tax_calculated = result['tax_amount']
            tax.calculated_amount = tax_calculated
            tax.save(update_fields=['base_amount', 'calculated_amount'])
            
            quote_tax_total += tax_calculated
            current_item_total = result['total_amount']

            key = f"{tax.tax_type.tax_name}_{tax.selected_percentage}"
            if key not in breakdown_dict:
                breakdown_dict[key] = {
                    "tax_name": tax.tax_type.tax_name,
                    "percentage": str(tax.selected_percentage),
                    "base": Decimal("0.00"),
                    "tax_amount": Decimal("0.00"),
                }
            breakdown_dict[key]["base"] += current_item_total - tax_calculated  # original base
            breakdown_dict[key]["tax_amount"] += tax_calculated

        # Format breakdown for JSON
        final_breakdown = []
        total_tax_amount = Decimal("0.00")
        for k, v in breakdown_dict.items():
            final_breakdown.append({
                "tax_name": v["tax_name"],
                "percentage": str(v["percentage"]),
                "base": str(v["base"].quantize(Decimal("0.01"))),
                "tax_amount": str(v["tax_amount"].quantize(Decimal("0.01"))),
            })
            total_tax_amount += v["tax_amount"]

        self.tax_breakdown = final_breakdown
        self.tax_amount = total_tax_amount
        
        config_total_with_tax = sum(c.total_price.amount for c in self.configurations.all())
        items_total_with_tax = item_total + quote_tax_total

        self.total_price = config_total_with_tax + items_total_with_tax
        self.save(update_fields=['subtotal_price', 'tax_amount', 'total_price', 'tax_breakdown'])

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

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)
        self.quote.recalc_totals()


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
