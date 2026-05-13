from django.db import models
from django.db.models import Sum
from company.models import  CompanyEmailConfig, CompanyOwnedModel
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from materials.models import Provider
from django_countries.fields import CountryField
from django.utils.translation import gettext_lazy as _ 
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.validators import FileExtensionValidator
from djmoney.models.fields import MoneyField
from djmoney.money import Money
from company.models import Company
from django.utils import timezone
import unicodedata
import re
from payments.models import PaymentAllocation

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

class ConfirmationStatus(models.TextChoices):
    APPROVED = 'approved', _('Approved')
    PENDING = 'pending', _('Pending Review')
    DECLINED = 'declined', _('Declined')

class InvoicePaymentStatus(models.TextChoices):
    UNPAID = "unpaid", _("Unpaid")
    PARTIAL = "partial", _("Partial")
    PAID = "paid", _("Paid")

def invoice_file_path(instance, filename):
    """
    Determines the path where the invoice file will be stored.
    Format: invoices/uploads/company_{id}/provider_{id}/{timestamp}_{filename}
    Handles cases where company or provider are not yet assigned.
    """
    timestamp = int(timezone.now().timestamp())
    
    company_folder = f"company_{instance.company_id}" if instance.company_id else "unknown_company"
    provider_folder = f"provider_{instance.provider_id}" if instance.provider_id else "unknown_provider"
    
    # Ensure unique filename with timestamp
    unique_filename = f"{timestamp}_{filename}"
    
    return f"invoices/uploads/{company_folder}/{provider_folder}/{unique_filename}"


class Invoice(models.Model):
    invoice_number = models.CharField(max_length=30,blank=True, null=True,verbose_name=_("Invoice number")) #numero de factura
    date_issued = models.DateField(db_index=True,blank=True, null=True,verbose_name=_("Date issued")) 
    due_date = models.DateField(blank=True, null=True,verbose_name=_("Due date")) #fecha de vencimiento
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT,blank=True, null=True,verbose_name=_("Provider")) 
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="invoices", null=True)
    
    account = models.ForeignKey(
        'accounting.CompanyAccount', 
        on_delete=models.PROTECT, 
        null=True,
        blank=True,
        related_name='invoices',
        help_text="Accounting account assigned by Gemini or manually overridden."
    )
    
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.TRANSFER
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_invoices",
        verbose_name=_("Project File")
    )

    observations = models.TextField(blank=True, null=True)

    payment_status = models.CharField(
        max_length=10,
        choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
        verbose_name=_("Payment Status"),
    )

    # Stores the path to the file, actual file saved locally
    file = models.FileField(
        upload_to=invoice_file_path,  
        validators=[
            FileExtensionValidator(
                allowed_extensions=['pdf', 'png', 'jpg', 'jpeg']
            )
        ],
        help_text="Upload invoice file (PDF, PNG, or JPG)",
        null=False,  
        blank=False  
    )
    gross_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Total invoice amount before discounts",
        default_currency='EUR',
        default=0
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

    needs_manual_review = models.BooleanField(
        default=True,  # Always starts as True
        help_text="Invoice requires manual review by user (always True initially, set to False after user review)"
    )

    confirmation_status = models.CharField(
        max_length=20,
        choices=ConfirmationStatus.choices,
        default=ConfirmationStatus.PENDING,
        help_text="Review status of this invoice"
    )

    image_hash = models.CharField(
        max_length=64, 
        db_index=True, 
        null=False, #DB level
        blank=True, #Form level
        help_text="Perceptual hash for duplicate detection"
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

    ocr_raw_response = models.JSONField(
        blank=True,
        null=True,
        help_text="Raw JSON response from Gemini for initial OCR"
    )
    
    gemini_account_response = models.JSONField(
        blank=True,
        null=True,
        help_text="Raw JSON response from Gemini for account classification"
    )

    is_total_balanced = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Check if the sum of line items and taxes matches the total_amount"
    )

    class Meta: 
        unique_together = ['provider','invoice_number']
        ordering = ['-date_issued']
        verbose_name = _("Invoice")
        verbose_name_plural = _("Invoices")

    def __str__(self):
        return f"{self.invoice_number} - {self.provider}"
    
    def recalculate_totals(self, save=True):
        """
        Recalculates the total amount by summing up items and applying taxes sequentially.
        """
        items_sum = self.items.aggregate(total=Sum('line_total'))['total']
        if items_sum is not None:
             base_amount = Decimal(str(items_sum.amount if hasattr(items_sum, 'amount') else items_sum))
        else:
             base_amount = Decimal("0.00")
             
        current_total = base_amount
        # Pull taxes in order to apply them on top
        taxes = list(self.get_taxes().order_by('order', 'id'))
        
        for tax in taxes:
            # Use the tax's own stored base_amount and calculated_amount
            # (set during OCR scraping). Only recalculate if calculated_amount is missing.
            if tax.calculated_amount is None:
                tax_base = Decimal(str(tax.base_amount.amount)) if tax.base_amount else base_amount
                res = tax.calculate(tax_base)
                tax.calculated_amount = res['tax_amount']
                if tax.base_amount is None:
                    tax.base_amount = tax_base
                tax.save(update_fields=['base_amount', 'calculated_amount'])
            
            # Use the signed calculated amount (preserves sign for negative/credit invoices)
            tax_val = Decimal(str(tax.calculated_amount.amount))

            if tax.tax_type.behavior == "add":
                current_total += tax_val
            else:
                current_total -= tax_val
            
        self.total_amount = current_total
        
        # When manually recalculating, we should also update the balance status
        self.verify_total_balance(save=False)
        
        if save:
            self.save(update_fields=['total_amount', 'is_total_balanced'])
            
        return self.total_amount

    def verify_total_balance(self, save=True):
        """
        Verifies if sum of line items + taxes matches total_amount.
        Updates is_total_balanced field.
        """
        items_sum = self.items.aggregate(total=Sum('line_total'))['total']
        if items_sum is not None:
            sum_lines = Decimal(str(items_sum.amount if hasattr(items_sum, 'amount') else items_sum))
        else:
            sum_lines = Decimal("0.00")

        sum_taxes = Decimal("0.00")
        for tax in self.get_taxes():
            if tax.calculated_amount:
                tax_val = Decimal(str(tax.calculated_amount.amount))
                if tax.tax_type.behavior == "add":
                    sum_taxes += tax_val
                else:
                    sum_taxes -= tax_val
        
        calc_total = sum_lines + sum_taxes
        
        tolerance = Decimal("0.01")
        
        stored_total = Decimal(str(self.total_amount.amount)) if self.total_amount else Decimal("0.00")
        stored_gross = Decimal(str(self.gross_amount.amount)) if self.gross_amount else Decimal("0.00")

        matches_total = abs(calc_total - stored_total) <= tolerance
        
        self.is_total_balanced = matches_total
        
        if save:
            self.save(update_fields=['is_total_balanced'])
            
        return self.is_total_balanced

    def save(self, *args, **kwargs):
        # Automatic recalculation if already exists
        if self.pk and not kwargs.get('update_fields'):
            self.recalculate_totals(save=False)
        super().save(*args, **kwargs)

    def recalculate_payment_status(self):
        """
        Recompute payment_status from PaymentAllocation entries pointing to this invoice.
        """
        

        ct = ContentType.objects.get_for_model(self)
        total_applied = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or 0
        )

        if total_applied <= 0:
            new_status = InvoicePaymentStatus.UNPAID
        elif self.total_amount and total_applied >= self.total_amount.amount:
            new_status = InvoicePaymentStatus.PAID
        else:
            new_status = InvoicePaymentStatus.PARTIAL

        self.payment_status = new_status
        self.save(update_fields=["payment_status"])

    @property
    def amount_paid(self):

        ct = ContentType.objects.get_for_model(self)
        total = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or 0
        )
        currency = self.total_amount_currency if self.total_amount else 'EUR'
        return Money(total, currency)

    @property
    def amount_remaining(self):
        if self.total_amount:
            return self.total_amount - self.amount_paid
        return Money(0, 'EUR')
    
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

 
class A3TaxGroup(models.TextChoices):
    IVA = "IVA", _("IVA")
    IRPF = "IRPF", _("IRPF")
    RE = "RE", _("Recargo de Equivalencia")
    NONE = "NONE", _("None")


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

    default_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Default percentage when applying this tax",
        default=0
    )
    
    is_debit = models.BooleanField(
        default=True,
        verbose_name=_("Journal as Debit"),
        help_text=_("In a purchase invoice, identify if this tax should be journalized on the Debit side (typical for VAT).")
    )
    
    a3_tax_group = models.CharField(
        max_length=10,
        choices=A3TaxGroup.choices,
        default=A3TaxGroup.NONE,
        help_text=_("Mapping for A3 export columns")
    )
    
    purchase_account = models.ForeignKey(
        "accounting.TemplateAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchase_tax_types",
        help_text=_("Generic template account used for this tax in Purchase Invoices (resolved per company).")
    )

    sales_account = models.ForeignKey(
        "accounting.TemplateAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_tax_types",
        help_text=_("Generic template account used for this tax in Sales Invoices (resolved per company).")
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

    @staticmethod
    def normalize_tax_name(name: str) -> str:
        """
        Normalizes tax names:
        - Strip accents
        - Uppercase
        - Remove non-alphanumeric characters (dots, spaces, etc.)
        - Strip leading/trailing spaces
        """
        if not name:
            return ""
        
        normalized = unicodedata.normalize('NFKD', name)
        ascii_name = normalized.encode('ascii', 'ignore').decode('ascii')
        
        clean_name = re.sub(r'[^A-Z0-9]', '', ascii_name.upper())
        
        return clean_name.strip()


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

    selected_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
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
        default=None
    )

    calculated_amount = MoneyField(
        max_digits=10,
        decimal_places=2,
        null=True,  # Allow null for existing records
        blank=True,  # Allow blank in forms
        help_text="The calculated tax amount",
        default_currency='EUR',
        default=None
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    order = models.PositiveIntegerField(default=0,db_index=True)

    # Fields whose modification is blocked on a finalized invoice.
    # Saves that only touch computed fields (base_amount, calculated_amount)
    # are allowed through so that recalculate_totals() can still run.
    _SENSITIVE_FIELDS = frozenset({"tax_type_id", "selected_percentage", "order"})

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError

        if self.content_type.model == 'salesinvoice':
            from invoices.models import SalesInvoiceStatus
            invoice = self.content_object
            if invoice and invoice.status == SalesInvoiceStatus.FINAL:
                update_fields = kwargs.get("update_fields")

                if not self.pk:
                    # New tax being added to a finalized invoice — always block.
                    raise ValidationError(_("Cannot edit taxes in a finalized invoice."))

                if update_fields is not None:
                    # Explicit partial save: only block if a sensitive field is included.
                    if self._SENSITIVE_FIELDS.intersection(update_fields):
                        raise ValidationError(_("Cannot edit taxes in a finalized invoice."))
                    # e.g. only base_amount / calculated_amount — allow through.
                else:
                    # Full save triggered by Django's inline formset machinery.
                    # Only block if a sensitive field has actually changed.
                    db = (
                        AppliedTax.objects.filter(pk=self.pk)
                        .values(*self._SENSITIVE_FIELDS)
                        .first()
                    )
                    if db and any(
                        getattr(self, field) != db[field]
                        for field in self._SENSITIVE_FIELDS
                    ):
                        raise ValidationError(_("Cannot edit taxes in a finalized invoice."))

        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "Applied Tax"
        verbose_name_plural = "Applied Taxes"
        ordering = ["content_type", "object_id", "order", "tax_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id", "tax_type", "selected_percentage"],
                name="uniq_tax_per_object_and_percentage",
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

    def calculate_from_gross(self, gross_amount: Decimal) -> dict:
        """
        Calculates the tax components backwards from a gross amount.
        Used for tax-inclusive prices.

        Returns:
        {
            "tax_amount": tax_amount,
            "base_amount": base
        }
        """
        gross_total = Decimal(gross_amount)
        rate = self.selected_percentage / Decimal("100")

        if self.tax_type.behavior == "add":
            # Gross = Base * (1 + Rate) => Base = Gross / (1 + Rate)
            base = (gross_total / (Decimal("1.0") + rate)).quantize(Decimal("0.01"))
            tax_amount = gross_total - base
        else:
            # Gross = Base * (1 - Rate) => Base = Gross / (1 - Rate)
            base = (gross_total / (Decimal("1.0") - rate)).quantize(Decimal("0.01"))
            tax_amount = base - gross_total

        return {
            "tax_amount": tax_amount,
            "base_amount": base
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
        result = self.calculate(self.base_amount.amount)
        
        # Store the calculated tax amount
        self.calculated_amount = result['tax_amount']
        
        # Save to database
        self.save(update_fields=['calculated_amount', 'base_amount'])
        
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
    is_duplicate = models.BooleanField(default=False, verbose_name=_("Is Duplicate"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Incoming Invoice")
        verbose_name_plural = _("Incoming Invoices")

    def __str__(self):
        return f"Incoming Invoice {self.file_hash[:8]} ({self.company})"

class SalesInvoiceStatus(models.TextChoices):
    PROFORMA = "proforma", _("Pro Forma")
    FINAL = "final", _("Final")


class SalesInvoice(CompanyOwnedModel):
    status = models.CharField(
        max_length=20, 
        choices=SalesInvoiceStatus.choices, 
        default=SalesInvoiceStatus.PROFORMA,
        verbose_name=_("Status")
    )
    invoice_number = models.CharField(max_length=50, blank=True, null=True, verbose_name=_("Invoice Number"))
    date_issued = models.DateField(default=timezone.now, verbose_name=_("Date Issued"))
    due_date = models.DateField(null=True, blank=True, verbose_name=_("Due Date"))
    client = models.ForeignKey(
        'sales.Client', 
        on_delete=models.PROTECT, 
        related_name='sales_invoices',
        verbose_name=_("Client")
    )
    incremental_id = models.PositiveIntegerField(null=True, blank=True, verbose_name=_("Incremental ID"))
    configuration = models.ForeignKey(
        'configurator.Configuration', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='sales_invoices',
        verbose_name=_("Configuration")
    )
    project_file = models.ForeignKey(
        "management.ProjectFile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_invoices",
        verbose_name=_("Project File")
    )
    
    subtotal_amount = MoneyField(
        max_digits=12, 
        decimal_places=2, 
        default=0, 
        default_currency='EUR',
        verbose_name=_("Subtotal")
    )
    tax_amount = MoneyField(
        max_digits=12, 
        decimal_places=2, 
        default=0, 
        default_currency='EUR',
        verbose_name=_("Tax Amount")
    )
    total_amount = MoneyField(
        max_digits=12, 
        decimal_places=2, 
        default=0, 
        default_currency='EUR',
        verbose_name=_("Total Amount")
    )
    
    reference = models.CharField(max_length=255, blank=True, null=True, verbose_name=_("Reference"))

    notes = models.TextField(blank=True, null=True, verbose_name=_("Notes"))

    payment_status = models.CharField(
        max_length=10,
        choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
        verbose_name=_("Payment Status"),
    )

    class Meta:
        verbose_name = _("Sales Invoice")
        verbose_name_plural = _("Sales Invoices")
        ordering = ['-date_issued', '-invoice_number']
        unique_together = ['company', 'invoice_number']

    def __str__(self):
        if self.status == SalesInvoiceStatus.PROFORMA:
            return f"Pro Forma #{self.id} - {self.client}"
        return f"{self.invoice_number} - {self.client}"

    def recalculate_totals(self, save=True):
        """
        Recalculates the total and tax amounts by applying taxes sequentially
        on top of the subtotal (which is summed from line items).
        
        If the status is FINAL, no recalculation is performed to preserve history.
        """
        if self.status == SalesInvoiceStatus.FINAL:
            return {
                'subtotal': self.subtotal_amount,
                'tax': self.tax_amount,
                'total': self.total_amount
            }

        # 1. Update subtotal from items if they exist
        items_sum = self.items.aggregate(total=Sum('line_total'))['total']
        if items_sum is not None:
             # Ensure subtotal_amount gets the numeric value
             self.subtotal_amount = items_sum.amount if hasattr(items_sum, 'amount') else items_sum
             
        ct = ContentType.objects.get_for_model(self)
        current_total = Decimal(self.subtotal_amount.amount)
        
        # 2. Sequential tax application (Additive)
        taxes = list(AppliedTax.objects.filter(content_type=ct, object_id=self.pk).order_by('order', 'id'))
        
        total_tax = Decimal("0.00")
        for tax in taxes:
            # Always update the base_amount for this tax
            tax.base_amount = self.subtotal_amount
            
            # Recalculate the amount
            res = tax.calculate(self.subtotal_amount.amount)
            tax.calculated_amount = res['tax_amount']
            tax.save(update_fields=['base_amount', 'calculated_amount'])
                
            amt = abs(tax.calculated_amount.amount)
            
            if tax.tax_type.behavior == "add":
                total_tax += amt
                current_total += amt
            else:
                total_tax -= amt
                current_total -= amt
            
        self.tax_amount = total_tax
        self.total_amount = current_total
        
        if save:
            self.save(update_fields=['subtotal_amount', 'tax_amount', 'total_amount'])
        
        return {
            'subtotal': self.subtotal_amount,
            'tax': self.tax_amount,
            'total': self.total_amount
        }

    def recalculate_payment_status(self):
        """
        Recompute payment_status from PaymentAllocation entries pointing to this invoice.
        Always call this inside a transaction.atomic() block.
        """
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(self)
        total_applied = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or 0
        )

        if total_applied <= 0:
            new_status = InvoicePaymentStatus.UNPAID
        elif total_applied >= self.total_amount.amount:
            new_status = InvoicePaymentStatus.PAID
        else:
            new_status = InvoicePaymentStatus.PARTIAL

        self.payment_status = new_status
        self.save(update_fields=["payment_status"])

    @property
    def amount_paid(self):
        from payments.models import PaymentAllocation

        ct = ContentType.objects.get_for_model(self)
        total = (
            PaymentAllocation.objects.filter(content_type=ct, object_id=self.pk)
            .aggregate(total=Sum("amount_applied"))["total"]
            or 0
        )
        return Money(total, self.total_amount_currency)

    @property
    def amount_remaining(self):
        return self.total_amount - self.amount_paid

    def save(self, *args, **kwargs):
        # Automatic recalculation if already exists
        if self.pk and not kwargs.get('update_fields'):
            self.recalculate_totals(save=False)
        super().save(*args, **kwargs)

class SalesInvoiceItem(models.Model):
    invoice = models.ForeignKey(
        SalesInvoice, 
        on_delete=models.CASCADE, 
        related_name='items',
        verbose_name=_("Invoice")
    )
    description = models.CharField(max_length=500, verbose_name=_("Description"))
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1, verbose_name=_("Quantity"))
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        verbose_name=_("Discount (%)")
    )
    unit_price = MoneyField(
        max_digits=12, 
        decimal_places=2, 
        default_currency='EUR',
        verbose_name=_("Unit Price")
    )
    discount_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Discount")
    )
    line_total = MoneyField(
        max_digits=12, 
        decimal_places=2, 
        default_currency='EUR',
        verbose_name=_("Line Total")
    )

    def save(self, *args, **kwargs):
        if self.invoice.status == SalesInvoiceStatus.FINAL and self.pk:
            from django.core.exceptions import ValidationError
            raise ValidationError(_("Cannot edit items in a finalized invoice."))
            
        self.discount_amount = (self.unit_price * self.quantity) * (self.discount_percentage / Decimal("100"))
        self.line_total = (self.unit_price * self.quantity) - self.discount_amount
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} ({self.quantity})"

class InvoiceItem(models.Model):
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name='items',
        verbose_name=_("Invoice")
    )
    description = models.CharField(max_length=255, verbose_name=_("Description"))
    quantity = models.DecimalField(max_digits=10, decimal_places=3, default=1, verbose_name=_("Quantity"))
    unit_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency='EUR',
        verbose_name=_("Unit Price")
    )
    discount_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))],
        verbose_name=_("Discount (%)")
    )
    discount_amount = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Discount Amount")
    )
    gross_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency='EUR',
        verbose_name=_("Gross Total")
    )
    line_total = MoneyField(
        max_digits=12,
        decimal_places=2,
        default_currency='EUR',
        verbose_name=_("Line Total")
    )

    account = models.ForeignKey(
        'accounting.CompanyAccount',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='invoice_items',
        help_text="Accounting account assigned by Gemini or manually overridden for this specific line item."
    )

    def save(self, *args, **kwargs):
        self.gross_total = self.unit_price * self.quantity
        self.discount_amount = self.gross_total * (self.discount_percentage / Decimal("100"))
        self.line_total = self.gross_total - self.discount_amount
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} ({self.quantity})"

    class Meta:
        verbose_name = "Invoice Item"
        verbose_name_plural = "Invoice Items"