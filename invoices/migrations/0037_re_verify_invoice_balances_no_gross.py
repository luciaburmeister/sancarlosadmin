from django.db import migrations, models
from django.db.models import Sum
from decimal import Decimal

def re_verify_balances(apps, schema_editor):
    Invoice = apps.get_model('invoices', 'Invoice')
    InvoiceItem = apps.get_model('invoices', 'InvoiceItem')
    AppliedTax = apps.get_model('invoices', 'AppliedTax')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    
    invoice_ct = ContentType.objects.get_for_model(Invoice)
    
    for invoice in Invoice.objects.all():
        # Calculate sum of items
        items_sum = InvoiceItem.objects.filter(invoice=invoice).aggregate(total=Sum('line_total'))['total']
        if items_sum is not None:
            sum_lines = Decimal(str(items_sum.amount if hasattr(items_sum, 'amount') else items_sum))
        else:
            sum_lines = Decimal("0.00")
            
        # Calculate sum of taxes
        taxes = AppliedTax.objects.filter(
            content_type=invoice_ct,
            object_id=invoice.id
        )
        
        sum_taxes = Decimal("0.00")
        for tax in taxes:
            if tax.calculated_amount:
                behavior = tax.tax_type.behavior
                amt = abs(tax.calculated_amount.amount)
                if behavior == "add":
                    sum_taxes += amt
                else:
                    sum_taxes -= amt
                    
        calc_total = sum_lines + sum_taxes
        tolerance = Decimal("0.01")
        
        stored_total = Decimal(str(invoice.total_amount.amount)) if invoice.total_amount else Decimal("0.00")
        
        # Only check total_amount now
        invoice.is_total_balanced = abs(calc_total - stored_total) <= tolerance
        invoice.save(update_fields=['is_total_balanced'])

class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0036_update_invoice_balance_status'),
    ]

    operations = [
        migrations.RunPython(re_verify_balances, reverse_code=migrations.RunPython.noop),
    ]
