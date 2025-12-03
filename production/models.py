from django.db import models
from inventory.models import Material
from sales.models import Order
from djmoney.models.fields import MoneyField

class ProductModel(models.Model):
    """
    Represents the 'Type' of Excel sheet.
    e.g., '2 HOJAS ABATIBLES', 'FIJO LAMAS 100', 'CORREDERA'
    """
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    
    # Example: 'logic.strategies.TwoLeafDoorStrategy'
    logic_handler_path = models.CharField(max_length=255, help_text="Path to the python class that calculates cuts")

    def __str__(self):
        return self.name

class ManufacturingItem(models.Model):
    """
    Represents a specific line item in the Order.
    Matches the 'MEDIDAS' section of your Excel.
    """
    order = models.ForeignKey(Order, related_name='manufacturing_items', on_delete=models.CASCADE)
    product_model = models.ForeignKey(ProductModel, on_delete=models.PROTECT)
    width = models.PositiveIntegerField(help_text="Width in mm")
    height = models.PositiveIntegerField(help_text="Height in mm")
    quantity = models.PositiveIntegerField(default=1)
    finish_ral = models.CharField(max_length=50, default="#000000", help_text="Lacado/Color")    
    observations = models.TextField(blank=True)

    def __str__(self):
        return f"{self.product_model.name} ({self.width}x{self.height})"

    def calculate_cut_list(self):
        """
        This method would trigger the logic engine (replacing Excel formulas)
        to generate the CutListEntry objects below.
        """
        pass

class CutAngles(models.TextChoices):
    RECTO = "90/90", "Recto (90/90)"
    Inglete = "45/45", "Inglete (45/45)"
    Escuadra = "90/45", "Escuadra (90/45)"

class CutListEntry(models.Model):
    """
    Represents the calculated rows in your 'DESPIECE' section.
    This is the output of the system.
    """
    manufacturing_item = models.ForeignKey(ManufacturingItem, related_name='cut_list', on_delete=models.CASCADE)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    
    description = models.CharField(max_length=100, help_text="e.g. Hoja Lama (Alto)")
    units_needed = models.DecimalField(max_digits=6, decimal_places=2, help_text="How many pieces")
    measure_mm = models.PositiveIntegerField(help_text="Length of the cut in mm")
    cut_type = models.CharField(max_length=10, choices=CutAngles, default='90/90')
    cost_snapshot = MoneyField(max_digits=10, decimal_places=2, help_text="Cost at time of calculation", default_currency="EUR")

    def __str__(self):
        return f"{self.description}: {self.units_needed} x {self.measure_mm}mm"

class LaborEntry(models.Model):
    """
    Represents the 'CONTROL OPERARIOS' or 'M/O FABRICACION' section.
    """
    manufacturing_item = models.ForeignKey(ManufacturingItem, related_name='labor_list', on_delete=models.CASCADE)
    task_name = models.CharField(max_length=100, help_text="e.g. Corte, Montaje")
    hours = models.DecimalField(max_digits=5, decimal_places=2)
    hourly_rate = MoneyField(max_digits=6, decimal_places=2,default_currency="EUR")

    @property
    def total_cost(self):
        return self.hours * self.hourly_rate