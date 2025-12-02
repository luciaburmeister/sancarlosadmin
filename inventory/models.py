from django.db import models

class Category(models.Model):
    """
    E.g., 'Aluminum Profiles', 'Hardware', 'Paint', 'Labor'
    """
    name = models.CharField(max_length=100)
    
    class Meta:
        verbose_name_plural = "Categories"

    def __str__(self):
        return self.name

class UnitChoices(models.TextChoices):
    METERS = "m"
    UNITS = "u"
    KILOGRAMS = "kg"
    HOURS = "h"

class Material(models.Model):
    """
    Represents the database of items found in your 'MATERIALES' section.
    Matches Excel columns: REFERENCIA, PRECIO.
    """


    reference_code = models.CharField(max_length=50, unique=True, help_text="e.g., 4058, 4269")
    name = models.CharField(max_length=200, help_text="e.g., CERCO 50 mm, LAMA 200")
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    unit_type = models.CharField(max_length=2, choices=UnitChoices.choices, default='m')
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=4, help_text="Base cost from supplier")
    
    # Specific fields for Profiles (Aluminum) to help with weight/optimization
    is_profile = models.BooleanField(default=False, help_text="Check if this is a bar that needs cutting")
    bar_length = models.DecimalField(max_digits=6, decimal_places=0, null=True, blank=True, help_text="Standard length of the bar in mm (e.g. 6000)")

    def __str__(self):
        return f"[{self.reference_code}] {self.name}"