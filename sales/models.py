from django.db import models
from djmoney.models.fields import MoneyField
from materials.models import Material
from timestamps.models import Timestampable

class Client(Timestampable):
    name = models.CharField(max_length=200, help_text="e.g., SEBASTIAN HERRERO")
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    def __str__(self):
        return self.name
    
class OrderStatus(models.TextChoices):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    PRODUCTION = "production"
    COMPLETED = "completed"
class Order(Timestampable):
    """
    Represents the Excel File itself (The Header info).
    """

    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    project_name = models.CharField(max_length=200, help_text="Obra / Site name")
    date_created = models.DateField(auto_now_add=True)
    deadline_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.DRAFT)
    
    # Financials (Calculated fields)
    total_cost_material = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR")
    total_cost_labor = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR")
    total_cost_machine = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR")
    final_price = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR",editable=False)


    def recalc_totals(self):
        items = self.items.all()
        total = sum(item.line_total.amount for item in items)

        self.total_material_cost = total
        self.final_price = self.total_material_cost + self.total_labor_cost
        self.save(update_fields=["total_material_cost", "final_price"])
    def __str__(self):
        return f"{self.project_name} - {self.client}"
    
class OrderItem(Timestampable):
    """
    A line item inside an order.
    """

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name="items"
    )
    material = models.ForeignKey(
        Material,
        on_delete=models.PROTECT,
        help_text="Material used"
    )

    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        help_text="Price per unit"
    )

    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency="EUR",
        editable=False
    )

    class Meta:
        verbose_name = "Order Item"
        verbose_name_plural = "Order Items"

    def __str__(self):
        return f"{self.material} x {self.quantity}"

    def save(self, *args, **kwargs):
        self.line_total = self.unit_price * self.quantity
        super().save(*args, **kwargs)


class Quote(Timestampable):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    project_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateField(null=True, blank=True)

    # internal status
    status = models.CharField(
        max_length=20,
        choices=[("draft", "Draft"), ("sent", "Sent"), ("accepted", "Accepted"), ("rejected", "Rejected")],
        default="draft",
    )

    total_price = MoneyField(max_digits=12, decimal_places=2, default_currency="EUR")

    def __str__(self):
        return f"Quote #{self.id} – {self.client}"

class QuoteItem(Timestampable):
    quote = models.ForeignKey(Quote, related_name="items", on_delete=models.CASCADE)
    material = models.ForeignKey("materials.Material", on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=10, decimal_places=3)
    unit_price = MoneyField(max_digits=12, decimal_places=2, default_currency="EUR")

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
