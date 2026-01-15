from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal
from materials.models import Provider



class InvoiceGroup(models.Model):
    """Categories to classify invoices"""
    name = models.CharField(max_length=100, unique=True) #stores the name of the group, must be unique
    
    class Meta:
        ordering = ['name']
        verbose_name = "Invoice Group"
        verbose_name_plural = "Invoice Groups"
    
    def __str__(self):
        return self.name



class Invoice(models.Model):
    invoice_number = models.CharField(max_length=30) #numero de factura
    date_issued = models.DateField(db_index=True) 
    due_date = models.DateField() #fecha de vencimiento
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT) 
    # Classification
    group = models.ForeignKey(
        InvoiceGroup, 
        on_delete=models.PROTECT, 
        related_name='invoices', 
        null=True, #makes having a group optional
        blank=True
    )
    # Payment details
    PAYMENT_METHOD_CHOICES = [
        ('transfer', 'Bank Transfer'),
        ('cash', 'Cash'),
        ('card', 'Credit/Debit Card'),
    ]
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES, default='transfer')
    observations = models.TextField(blank=True, null=True)

    class Meta: #"rulebook", how we store the data
        unique_together = ['provider','invoice_number'] # Ensures that a specific provider cannot have two invoices with the same number
        ordering = ['-date_issued'] #newest first
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"

    def __str__(self):
        return f"{self.invoice_number} - {self.provider}"
    

 
class TaxType(models.Model):
    """Stores different tax types (VAT, IRPF, etc.) with their rate options"""
    tax_name = models.CharField(
        max_length=50, 
        unique=True, 
        help_text="e.g., VAT, IRPF"
    )

    behavior = models.CharField(
        max_length=10,
        default='add',
        help_text="Whether this tax adds or subtracts from the base (backend only)"
    )

    percentage_options = models.JSONField(
        help_text="Available percentage rates as a list, e.g., [0, 4, 10, 21]"
    )

    default_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2,
        help_text="Default percentage when creating new invoice tax"
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['tax_name']
        verbose_name = "Tax Type"
        verbose_name_plural = "Tax Types"
    
    def __str__(self):
        return self.tax_name
    
    def get_percentage_choices(self):
        """
        Returns Django-compatible choices for the percentage dropdown
        Format: [(0, '0%'), (4, '4%'), (10, '10%'), (21, '21%')]
        """
        return [(Decimal(str(rate)), f"{rate}%") for rate in self.percentage_options]



class InvoiceTax(models.Model):
    """Tax calculations for each invoice"""
    #if the invoice is deleted, delete the tax records too
    invoice = models.ForeignKey( Invoice, on_delete=models.CASCADE, related_name='taxes')

    # Base amount for this tax calculation
    tax_base = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Base amount to calculate tax on"
    )

    # Step 1: User selects tax type (VAT, IRPF, etc.)
    tax_type = models.ForeignKey(
        TaxType, 
        on_delete=models.PROTECT, 
        related_name='invoice_taxes',
        help_text="Select the tax type"
    )

    # Step 2: User selects percentage from available options
    selected_percentage = models.DecimalField(
        max_digits=5, 
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.00'))],
        help_text="Selected tax rate percentage"
    )

    # Auto-calculated fields
    tax_amount = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        editable=False,
        help_text="Calculated: tax_base * (selected_percentage/100)"
    )

    total_amount = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        editable=False,
        help_text="Calculated: tax_base ± tax_amount (based on behavior)"
    )

    class Meta:
        ordering = ['invoice', 'tax_type']
        verbose_name = "Invoice Tax"
        verbose_name_plural = "Invoice Taxes"
        unique_together = ['invoice', 'tax_type']

    def __str__(self):
        return f"{self.invoice.invoice_number} - {self.tax_type.tax_name} ({self.selected_percentage}%)"

    def save(self, *args, **kwargs):
        """Auto-calculate tax_amount and total_amount"""
        # Calculate tax amount
        rate_decimal = self.selected_percentage / Decimal('100')
        self.tax_amount = (self.tax_base * rate_decimal).quantize(Decimal('0.01'))
        
        # Apply based on behavior (add or subtract)
        if self.tax_type.behavior == 'add':
            self.total_amount = (self.tax_base + self.tax_amount).quantize(Decimal('0.01'))
        else:  # subtract
            self.total_amount = (self.tax_base - self.tax_amount).quantize(Decimal('0.01'))
        
        super().save(*args, **kwargs)



