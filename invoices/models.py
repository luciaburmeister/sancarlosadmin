from django.db import models
from company.models import Company, CompanyEmailConfig, CompanyOwnedModel
from django.core.validators import MinValueValidator
from decimal import Decimal
from materials.models import Provider
from django_countries.fields import CountryField
from django.utils.translation import gettext_lazy as _ 
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import FileExtensionValidator
from djmoney.models.fields import MoneyField


class InvoiceGroup(models.Model):
    """Categories to classify invoices"""
    name = models.CharField(max_length=100, unique=True) #stores the name of the group, must be unique
    
    class Meta:
        ordering = ['name']
        verbose_name = "Invoice Group"
        verbose_name_plural = "Invoice Groups"
    
    def __str__(self):
        return self.name



class PaymentMethod(models.TextChoices):
    TRANSFER = 'transfer', _('Bank Transfer')  
    CASH = 'cash', _('Cash')
    CARD = 'card', _('Credit/Debit Card')



def invoice_file_path(instance, filename):
    """
    Generates organized upload path: invoices/YYYY/MM/filename
    Example: invoices/2024/01/invoice_123.pdf
    """
    return f"invoices/{instance.date_issued.year}/{instance.date_issued.month:02d}/{filename}"



class Invoice(models.Model):
    invoice_number = models.CharField(max_length=30) #numero de factura
    date_issued = models.DateField(db_index=True) 
    due_date = models.DateField() #fecha de vencimiento
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT) 
    
    group = models.ForeignKey(
        InvoiceGroup, 
        on_delete=models.PROTECT, 
        related_name='invoices', 
        null=True, #makes having a group optional
        blank=True
    )
    
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER
    )

    observations = models.TextField(blank=True, null=True)

    # Stores the path to the file, actual file saved locally
    file = models.FileField(
        upload_to=invoice_file_path,  
        validators=[
            FileExtensionValidator(
                allowed_extensions=['pdf', 'png', 'jpg', 'jpeg']
            )
        ],
        help_text="Upload invoice file (PDF, PNG, or JPG)",
        null=True,  # Temporarily allow null for existing invoices
        blank=True  # Temporarily allow blank in forms
    )

    total_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total invoice amount",
        default_currency='EUR',
        default=0
    )

    currency = models.CharField(
        max_length=3,
        default='EUR',
        help_text="Currency code (EUR, USD, etc.)"
    )

    line_items = models.JSONField(
        default=list,
        blank=True,
        help_text= "Line items with descriptions, quantities, prices, and tax rates"
    )

    needs_manual_review = models.BooleanField(
        default=True,  # Always starts as True
        help_text="Invoice requires manual review by user (always True initially, set to False after user review)"
    )
    
    # OCR STATUS FIELDS
    ocr_processed = models.BooleanField(
        default=False,
        help_text="Has OCR been attempted?"
    )
    
    ocr_processing = models.BooleanField(
        default=False,
        help_text="Is OCR running right now?"
    )
    
    ocr_error = models.TextField(
        blank=True,
        null=True,
        help_text="Error if OCR failed"
    )

    class Meta: #"rulebook", how we store the data
        unique_together = ['provider','invoice_number'] # Ensures that a specific provider cannot have two invoices with the same number
        ordering = ['-date_issued'] #newest first
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"

    def __str__(self):
        return f"{self.invoice_number} - {self.provider}"
    
    def get_taxes(self):
        ct = ContentType.objects.get_for_model(self)
        return AppliedTax.objects.filter(
            content_type=ct,
            object_id=self.pk
        ).select_related("tax_type").order_by("order")

    def apply_taxes(self, base_amount: Decimal) -> Decimal:
        total = base_amount

        for tax in self.get_taxes():
            result = tax.calculate(total)
            total = result["total_amount"]

        return total

 
class TaxType(models.Model):
    """
    Stores different tax types (VAT, IRPF, etc.) with their rate options.
    This is a tax definition (rules + allowed rates).
    """
    tax_name = models.CharField(
        max_length=50,
        help_text="e.g., VAT, IRPF"
    )

    country = CountryField(
        default="ES",
        help_text="Country where this tax applies"
    )

    behavior = models.CharField(
        max_length=10,
        choices=[("add", _("Add")), ("subtract", _("Subtract"))],
        default="add",
        help_text="Whether this tax adds or subtracts from the base"
    )

    percentage_options = models.JSONField(
        default=list,
        help_text="Available percentage rates as a list, e.g., [0, 4, 10, 21]"
    )

    default_percentage = MoneyField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Default percentage when applying this tax",
        default_currency='EUR',
        default=0
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["tax_name"]
        verbose_name = "Tax Type"
        verbose_name_plural = "Tax Types"
        unique_together = ("tax_name", "country")  # ✅ important for multi-country

    def __str__(self):
        return f"{self.tax_name} ({self.country.code})"

    def get_percentage_choices(self):
        return [(Decimal(str(rate)), f"{rate}%") for rate in (self.percentage_options or [])]


class AppliedTax(models.Model):
    """
    A tax applied to ANY entity (Invoice, Provider, ProductTemplate, Configuration, etc.)
    via a GenericForeignKey.
    """

    tax_type = models.ForeignKey(
        TaxType,
        on_delete=models.PROTECT,
        related_name="applied_taxes",
    )

    selected_percentage = MoneyField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        default_currency='EUR',
        default=0
    )

    #Base amount each tax is calculated on
    base_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,  # Allow null for existing records
        blank=True,  # Allow blank in forms
        help_text="The base amount this tax was calculated on",
        default_currency='EUR',
        default=0
    )

    calculated_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,  # Allow null for existing records
        blank=True,  # Allow blank in forms
        help_text="The calculated tax amount",
        default_currency='EUR',
        default=0
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    order = models.PositiveIntegerField(default=0,db_index=True)

    class Meta:
        verbose_name = "Applied Tax"
        verbose_name_plural = "Applied Taxes"
        ordering = ["content_type", "object_id", "order", "tax_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id", "tax_type"],
                name="uniq_tax_per_object",
            ),
        ]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["content_type", "object_id","order"]),
        ]

    def __str__(self):
        return f"{self.content_object} – {self.tax_type} ({self.selected_percentage}%)"

    def calculate(self, base_amount: Decimal) -> dict:
        """
        Returns:
        {
            tax_amount: Decimal,
            total_amount: Decimal
        }
        """
        base_amount = Decimal(base_amount)
        rate = self.selected_percentage / Decimal("100")
        tax_amount = (base_amount * rate).quantize(Decimal("0.01"))

        if self.tax_type.behavior == "add":
            total = base_amount + tax_amount
        else:
            total = base_amount - tax_amount

        return {
            "tax_amount": tax_amount,
            "total_amount": total.quantize(Decimal("0.01")),
        }

    def calculate_and_store(self):
        """
        Calculate tax using the stored base_amount and save the result to calculated_amount.
        Raises ValueError if base_amount is not set.
        """
        # Check if base_amount exists
        if self.base_amount is None:
            raise ValueError(
                f"Cannot calculate tax for {self}: base_amount is not set. "
                "Set base_amount before calling calculate_and_store()."
            )
        
        # Use the existing calculate method
        result = self.calculate(self.base_amount)
        
        # Store the calculated tax amount
        self.calculated_amount = result['tax_amount']
        
        # Save to database
        self.save(update_fields=['calculated_amount'])
        
        return result
def incoming_invoice_upload_path(instance, filename):
    """Path: incoming_invoices/year/month/day/filename"""
    date = instance.email_date or instance.created_at or timezone.now()
    return f"incoming_invoices/{date.strftime('%Y/%m/%d')}/{filename}"

class IncomingInvoice(CompanyOwnedModel):
    email_config = models.ForeignKey(CompanyEmailConfig, on_delete=models.SET_NULL, null=True, related_name="downloaded_invoices")
    file = models.FileField(upload_to=incoming_invoice_upload_path, verbose_name=_("Invoice File"))
    email_date = models.DateTimeField(null=True, blank=True, verbose_name=_("Email Date"))
    file_hash = models.CharField(max_length=64, unique=True, verbose_name=_("File Hash (SHA-256)"))
    source_email_id = models.CharField(max_length=255, verbose_name=_("Source Email ID"))
    sender_email = models.EmailField(null=True, blank=True, verbose_name=_("Sender Email"))
    processed = models.BooleanField(default=False, verbose_name=_("Processed by AI"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Incoming Invoice")
        verbose_name_plural = _("Incoming Invoices")

    def __str__(self):
        return f"Incoming Invoice {self.file_hash[:8]} ({self.company})"
