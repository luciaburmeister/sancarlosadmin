from django.db import models
from django.utils import timezone
from djmoney.models.fields import MoneyField
from materials.models import Material
from timestamps.models import Timestampable
from django.utils.translation import gettext_lazy as _
from company.models import CompanyOwnedModel

class Client(Timestampable, CompanyOwnedModel):
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
    )
    tax_id = models.CharField(
        max_length=25,
        blank=True,
        verbose_name=_("Tax ID"),
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

class Order(Timestampable):
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

    # Financials
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


class Quote(Timestampable):
    client = models.ForeignKey(
        Client,
        on_delete=models.CASCADE,
        verbose_name=_("Client"),
    )

    project_name = models.CharField(
        max_length=200,
        verbose_name=_("Project name"),
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created at"),
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

    total_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        verbose_name=_("Total price"),
    )

    class Meta:
        verbose_name = _("Quote")
        verbose_name_plural = _("Quotes")

    def __str__(self):
        return f"{_('Quote')} #{self.id} – {self.client}"

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

    class Meta:
        verbose_name = _("Quote item")
        verbose_name_plural = _("Quote items")


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
