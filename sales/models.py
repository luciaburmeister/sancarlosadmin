from django.db import models
from djmoney.models.fields import MoneyField


class Client(models.Model):
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
class Order(models.Model):
    """
    Represents the Excel File itself (The Header info).
    """

    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    project_name = models.CharField(max_length=200, help_text="Obra / Site name")
    date_created = models.DateField(auto_now_add=True)
    deadline_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.choices)
    
    # Financials (Calculated fields)
    total_cost_material = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR")
    total_cost_labor = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR")
    final_price = MoneyField(max_digits=12, decimal_places=2, default=0, default_currency="EUR")

    def __str__(self):
        return f"{self.project_name} - {self.client}"